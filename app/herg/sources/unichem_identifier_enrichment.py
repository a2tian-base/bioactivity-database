#!/usr/bin/env python3

from __future__ import annotations

import argparse
from dataclasses import asdict
import sys

from ..config import DbConfig, HttpConfig, IdentifierRunConfig
from ..identifier_pipeline import run_identifier_pipeline
from ..pipeline_common import write_stats
from .unichem_identifiers import DEFAULT_BATCH_SIZE, TARGET_SOURCE_IDS, UNICHEM_BASE_URL, UniChemIdentifierAdapter


def _build_db_config(args: argparse.Namespace) -> DbConfig:
    config = DbConfig.from_env()
    return DbConfig(
        host=args.db_host or config.host,
        port=args.db_port or config.port,
        dbname=args.db_name or config.dbname,
        user=args.db_user or config.user,
        password=args.db_password or config.password,
    )


def _target_namespaces(target_namespace: str | None) -> list[str]:
    if target_namespace:
        return [target_namespace]
    return list(TARGET_SOURCE_IDS)


def _log_namespace_summary(namespace: str, candidate_count: int, stats) -> None:
    _log("")
    _log("UniChem enrichment summary")
    _log("--------------------------")
    _log(f"Target namespace: {namespace}")
    _log(f"Candidate rows found: {candidate_count}")
    _log(f"Processed rows: {stats.processed}")
    _log(f"Attached: {stats.attached}")
    _log(f"Already present: {stats.already_present}")
    _log(f"Unmatched: {stats.unmatched}")
    _log(f"Conflicts: {stats.conflict}")
    _log(f"Skipped invalid: {stats.skipped_invalid}")
    _log(f"Failed rows: {stats.failed}")
    if candidate_count == 0:
        _log("No candidate compounds matched the current target namespace filter.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve and attach missing compound identifiers from UniChem.")
    parser.add_argument(
        "--target-namespace",
        choices=sorted(TARGET_SOURCE_IDS),
        default=None,
        help="If omitted, run all supported target namespaces sequentially.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-records", type=int, default=None, help="Maximum candidate rows to process per namespace.")
    parser.add_argument("--commit-every", type=int, default=500)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--request-timeout-seconds", type=int, default=45)
    parser.add_argument("--http-retries", type=int, default=4)
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--errors-path", default=None)
    parser.add_argument("--stats-path", default=None)
    parser.add_argument("--unmatched-path", default=None)
    parser.add_argument("--conflicts-path", default=None)
    parser.add_argument("--create-missing-compounds", action="store_true")
    parser.add_argument("--unichem-base-url", default=UNICHEM_BASE_URL)
    parser.add_argument("--db-host", default=None)
    parser.add_argument("--db-port", type=int, default=None)
    parser.add_argument("--db-name", default=None)
    parser.add_argument("--db-user", default=None)
    parser.add_argument("--db-password", default=None)
    return parser.parse_args()


def _log(message: str) -> None:
    print(message, flush=True)


def main() -> int:
    args = _parse_args()
    db_config = _build_db_config(args)
    namespaces = _target_namespaces(args.target_namespace)
    adapters: dict[str, UniChemIdentifierAdapter] = {}
    aggregate = {
        "target_namespaces": namespaces,
        "per_namespace": {},
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
            "created_compounds": 0,
        },
    }
    exit_code = 0

    _log("Scanning database for UniChem enrichment candidates...")
    for namespace in namespaces:
        adapter = UniChemIdentifierAdapter(
            http_config=HttpConfig(
                request_timeout_seconds=args.request_timeout_seconds,
                http_retries=args.http_retries,
            ),
            target_namespace=namespace,
            base_url=args.unichem_base_url,
            limit=args.max_records,
            enrich_batch_size=args.batch_size,
            db_config=db_config,
            progress_logger=_log,
        )
        adapters[namespace] = adapter
        candidate_count = adapter.load_candidates()
        aggregate["totals"]["candidate_rows_found"] += candidate_count
        _log(f" - {namespace}: {candidate_count} candidate rows")

    _log(f"Total candidate rows to process: {aggregate['totals']['candidate_rows_found']}")

    for namespace in namespaces:
        _log("")
        _log(f"Starting UniChem enrichment for {namespace}...")

        run_config = IdentifierRunConfig(
            dry_run=args.dry_run,
            max_records=args.max_records,
            commit_every=args.commit_every,
            fail_fast=args.fail_fast,
            errors_path=args.errors_path,
            stats_path=None,
            unmatched_path=args.unmatched_path,
            conflicts_path=args.conflicts_path,
            create_missing_compounds=args.create_missing_compounds,
        )
        adapter = adapters[namespace]

        stats = run_identifier_pipeline(adapter, db_config, run_config)
        _log_namespace_summary(namespace, adapter.last_candidate_count, stats)

        namespace_payload = {
            "candidate_rows_found": adapter.last_candidate_count,
            **asdict(stats),
        }
        aggregate["per_namespace"][namespace] = namespace_payload
        for key in aggregate["totals"]:
            if key == "candidate_rows_found":
                continue
            aggregate["totals"][key] += namespace_payload.get(key, 0)

        if stats.failed > 0 or stats.conflict > 0:
            exit_code = 1

    if len(namespaces) > 1:
        totals = aggregate["totals"]
        _log("")
        _log("Overall enrichment summary")
        _log("--------------------------")
        _log(f"Namespaces run: {', '.join(namespaces)}")
        _log(f"Candidate rows found: {totals['candidate_rows_found']}")
        _log(f"Processed rows: {totals['processed']}")
        _log(f"Attached: {totals['attached']}")
        _log(f"Already present: {totals['already_present']}")
        _log(f"Unmatched: {totals['unmatched']}")
        _log(f"Conflicts: {totals['conflict']}")
        _log(f"Skipped invalid: {totals['skipped_invalid']}")
        _log(f"Failed rows: {totals['failed']}")

    if len(namespaces) == 1:
        write_stats(args.stats_path, aggregate["per_namespace"][namespaces[0]])
    else:
        write_stats(args.stats_path, aggregate)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
