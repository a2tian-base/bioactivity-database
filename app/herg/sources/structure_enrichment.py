#!/usr/bin/env python3

from __future__ import annotations

import argparse
from dataclasses import asdict
import sys
from typing import Sequence

from ..config import DbConfig, HttpConfig, StructureRunConfig
from ..pipeline_common import write_stats
from ..read_db import fetch_structure_enrichment_candidates
from ..structure_pipeline import StructureEnrichmentStats, run_structure_pipeline
from .chembl_structures import CHEMBL_BASE_URL, ChemblStructureAdapter
from .pubchem_structures import PUBCHEM_BASE_URL, PubChemStructureAdapter


SUPPORTED_PROVIDERS = ("chembl", "pubchem")


def _log(message: str) -> None:
    print(message, flush=True)


def _build_db_config(args: argparse.Namespace) -> DbConfig:
    config = DbConfig.from_env()
    return DbConfig(
        host=args.db_host or config.host,
        port=args.db_port or config.port,
        dbname=args.db_name or config.dbname,
        user=args.db_user or config.user,
        password=args.db_password or config.password,
    )


def _parse_args(
    argv: Sequence[str] | None,
    *,
    default_provider: str,
    include_provider_arg: bool,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enrich compound structures from ChEMBL and PubChem.")
    if include_provider_arg:
        parser.add_argument(
            "--provider",
            choices=("all",) + SUPPORTED_PROVIDERS,
            default=default_provider,
            help="Structure source provider to run. Default: all.",
        )
    else:
        parser.set_defaults(provider=default_provider)

    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-records", type=int, default=None)
    parser.add_argument("--commit-every", type=int, default=500)
    parser.add_argument("--batch-size", type=int, default=150)
    parser.add_argument("--request-timeout-seconds", type=int, default=45)
    parser.add_argument("--http-retries", type=int, default=4)
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--errors-path", default=None)
    parser.add_argument("--unmatched-path", default=None)
    parser.add_argument("--conflicts-path", default=None)
    parser.add_argument("--stats-path", default=None)

    parser.add_argument("--chembl-base-url", default=CHEMBL_BASE_URL)
    parser.add_argument("--pubchem-base-url", default=PUBCHEM_BASE_URL)

    parser.add_argument("--db-host", default=None)
    parser.add_argument("--db-port", type=int, default=None)
    parser.add_argument("--db-name", default=None)
    parser.add_argument("--db-user", default=None)
    parser.add_argument("--db-password", default=None)
    return parser.parse_args(argv)


def _provider_order(provider: str) -> list[str]:
    if provider == "all":
        return list(SUPPORTED_PROVIDERS)
    return [provider]


def _candidate_count(provider: str, args: argparse.Namespace, db_config: DbConfig) -> int:
    return len(fetch_structure_enrichment_candidates(provider, limit=args.max_records, db_config=db_config))


def _build_adapter(provider: str, args: argparse.Namespace, http_config: HttpConfig, db_config: DbConfig):
    if provider == "chembl":
        return ChemblStructureAdapter(
            http_config=http_config,
            db_config=db_config,
            base_url=args.chembl_base_url,
            limit=args.max_records,
            molecule_batch_size=args.batch_size,
        )
    if provider == "pubchem":
        return PubChemStructureAdapter(
            http_config=http_config,
            db_config=db_config,
            base_url=args.pubchem_base_url,
            limit=args.max_records,
            cid_batch_size=args.batch_size,
        )
    raise ValueError(f"Unsupported provider '{provider}'.")


def _log_provider_summary(provider: str, candidate_count: int, stats: StructureEnrichmentStats) -> None:
    _log("")
    _log("Structure enrichment summary")
    _log("--------------------------")
    _log(f"Provider: {provider}")
    _log(f"Candidate rows found: {candidate_count}")
    _log(f"Processed rows: {stats.processed}")
    _log(f"Attached: {stats.attached}")
    _log(f"Already present: {stats.already_present}")
    _log(f"Unmatched: {stats.unmatched}")
    _log(f"Conflicts: {stats.conflict}")
    _log(f"Skipped invalid: {stats.skipped_invalid}")
    _log(f"Failed rows: {stats.failed}")
    if candidate_count == 0:
        _log("No candidate compounds matched the current provider filter.")


def main(
    argv: Sequence[str] | None = None,
    *,
    default_provider: str = "all",
    include_provider_arg: bool = True,
) -> int:
    args = _parse_args(argv, default_provider=default_provider, include_provider_arg=include_provider_arg)
    db_config = _build_db_config(args)
    http_config = HttpConfig(
        request_timeout_seconds=args.request_timeout_seconds,
        http_retries=args.http_retries,
    )
    providers = _provider_order(args.provider)
    aggregate = {
        "providers": providers,
        "per_provider": {},
        "totals": {
            "candidate_rows_found": 0,
            "processed": 0,
            "attached": 0,
            "already_present": 0,
            "unmatched": 0,
            "conflict": 0,
            "skipped_invalid": 0,
            "failed": 0,
            "warnings": 0,
        },
    }
    exit_code = 0

    _log("Scanning database for structure enrichment candidates...")
    candidate_counts: dict[str, int] = {}
    for provider in providers:
        count = _candidate_count(provider, args, db_config)
        candidate_counts[provider] = count
        aggregate["totals"]["candidate_rows_found"] += count
        _log(f" - {provider}: {count} candidate rows")
    _log(f"Total candidate rows to process: {aggregate['totals']['candidate_rows_found']}")

    for provider in providers:
        _log("")
        _log(f"Starting structure enrichment for {provider}...")

        run_config = StructureRunConfig(
            dry_run=args.dry_run,
            max_records=args.max_records,
            commit_every=args.commit_every,
            fail_fast=args.fail_fast,
            errors_path=args.errors_path,
            stats_path=None,
            unmatched_path=args.unmatched_path,
            conflicts_path=args.conflicts_path,
        )

        if candidate_counts[provider] == 0:
            stats = StructureEnrichmentStats()
        else:
            adapter = _build_adapter(provider, args, http_config, db_config)
            stats = run_structure_pipeline(adapter, db_config, run_config)

        _log_provider_summary(provider, candidate_counts[provider], stats)
        payload = {
            "candidate_rows_found": candidate_counts[provider],
            **asdict(stats),
        }
        aggregate["per_provider"][provider] = payload
        for key in aggregate["totals"]:
            if key == "candidate_rows_found":
                continue
            aggregate["totals"][key] += payload.get(key, 0)

        if stats.failed > 0 or stats.conflict > 0:
            exit_code = 1

    if len(providers) > 1:
        totals = aggregate["totals"]
        _log("")
        _log("Overall structure enrichment summary")
        _log("----------------------------------")
        _log(f"Providers run: {', '.join(providers)}")
        _log(f"Candidate rows found: {totals['candidate_rows_found']}")
        _log(f"Processed rows: {totals['processed']}")
        _log(f"Attached: {totals['attached']}")
        _log(f"Already present: {totals['already_present']}")
        _log(f"Unmatched: {totals['unmatched']}")
        _log(f"Conflicts: {totals['conflict']}")
        _log(f"Skipped invalid: {totals['skipped_invalid']}")
        _log(f"Failed rows: {totals['failed']}")

    if len(providers) == 1:
        write_stats(args.stats_path, aggregate["per_provider"][providers[0]])
    else:
        write_stats(args.stats_path, aggregate)
    return exit_code


def main_chembl(argv: Sequence[str] | None = None) -> int:
    return main(argv, default_provider="chembl", include_provider_arg=False)


def main_pubchem(argv: Sequence[str] | None = None) -> int:
    return main(argv, default_provider="pubchem", include_provider_arg=False)


if __name__ == "__main__":
    sys.exit(main())
