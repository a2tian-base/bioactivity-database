from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from typing import Any

from herg.config import DbConfig, HttpConfig, RunConfig
from herg.db import get_conn
from herg.normalize import clean_text
from herg.pipeline import IngestionStats, SourceAdapter, run_pipeline

from .endpoints import EndpointConfig, get_source_config, load_endpoint


SUPPORTED_SOURCES = frozenset({"chembl", "pubchem"})

AdapterFactory = Callable[[EndpointConfig, dict[str, Any], HttpConfig, dict[str, Any]], SourceAdapter]


def _clean_source_name(source_name: str) -> str:
    clean_source = clean_text(source_name).lower()
    if clean_source not in SUPPORTED_SOURCES:
        allowed = ", ".join(sorted(SUPPORTED_SOURCES))
        raise ValueError(f"Unsupported source '{source_name}'. Allowed: {allowed}.")
    return clean_source


def _non_null_overrides(overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    if not overrides:
        return {}
    return {key: value for key, value in overrides.items() if value is not None}


def _default_adapter_factory(
    endpoint: EndpointConfig,
    source_config: dict[str, Any],
    http_config: HttpConfig,
    options: dict[str, Any],
) -> SourceAdapter:
    source_name = _clean_source_name(str(options["source_name"]))
    if source_name == "chembl":
        from herg.sources.chembl import CHEMBL_BASE_URL, ChemblAdapter

        return ChemblAdapter.from_source_config(
            endpoint,
            source_config,
            http_config=http_config,
            base_url=str(options.get("chembl_base_url") or CHEMBL_BASE_URL),
            activity_page_size=options.get("activity_page_size"),
            molecule_batch_size=options.get("molecule_batch_size"),
        )
    if source_name == "pubchem":
        from herg.sources.pubchem import PUBCHEM_BASE_URL, PubChemAdapter

        return PubChemAdapter.from_source_config(
            endpoint,
            source_config,
            http_config=http_config,
            base_url=str(options.get("pubchem_base_url") or PUBCHEM_BASE_URL),
            cid_batch_size=options.get("cid_batch_size"),
        )
    raise AssertionError(f"Unhandled source '{source_name}'.")


def _call_pipeline(
    pipeline_runner: Callable[..., IngestionStats],
    adapter: SourceAdapter,
    db_config: DbConfig,
    run_config: RunConfig,
    *,
    endpoint_key: str,
) -> IngestionStats:
    return pipeline_runner(adapter, db_config, run_config, endpoint_key=endpoint_key)


def run_endpoint_ingestion(
    *,
    endpoint_key: str,
    source_name: str,
    db_config: DbConfig | None = None,
    http_config: HttpConfig | None = None,
    run_config: RunConfig | None = None,
    source_config_overrides: Mapping[str, Any] | None = None,
    chembl_base_url: str | None = None,
    pubchem_base_url: str | None = None,
    activity_page_size: int | None = None,
    molecule_batch_size: int | None = None,
    cid_batch_size: int | None = None,
    adapter_factories: Mapping[str, AdapterFactory] | None = None,
    pipeline_runner: Callable[..., IngestionStats] = run_pipeline,
) -> IngestionStats:
    """Run configured endpoint ingestion for a supported source.

    This is the primary endpoint-driven ingestion entry point. Legacy hERG
    scripts call this function with ``endpoint_key="herg_ic50"``.
    """
    clean_source = _clean_source_name(source_name)
    clean_endpoint_key = clean_text(endpoint_key)
    if not clean_endpoint_key:
        raise ValueError("endpoint_key is required.")

    resolved_db_config = db_config or DbConfig.from_env()
    resolved_http_config = http_config or HttpConfig()
    resolved_run_config = run_config or RunConfig()

    with get_conn(db_config=resolved_db_config) as conn:
        endpoint = load_endpoint(conn, clean_endpoint_key)

    source_config = get_source_config(endpoint, clean_source)
    source_config.update(_non_null_overrides(source_config_overrides))

    adapter_options = {
        "source_name": clean_source,
        "chembl_base_url": chembl_base_url,
        "pubchem_base_url": pubchem_base_url,
        "activity_page_size": activity_page_size,
        "molecule_batch_size": molecule_batch_size,
        "cid_batch_size": cid_batch_size,
    }
    factories = dict(adapter_factories or {})
    factory = factories.get(clean_source, _default_adapter_factory)
    adapter = factory(endpoint, source_config, resolved_http_config, adapter_options)

    return _call_pipeline(
        pipeline_runner,
        adapter,
        resolved_db_config,
        resolved_run_config,
        endpoint_key=endpoint.endpoint_key,
    )


def print_ingestion_summary(stats: IngestionStats, *, source_name: str) -> None:
    print("")
    print("Ingestion summary")
    print("-----------------")
    print(f"Source: {source_name}")
    print(f"Processed rows: {stats.processed}")
    print(f"Stored rows: {stats.stored}")
    print(f"Skipped invalid: {stats.skipped_invalid}")
    print(f"Failed inserts: {stats.failed}")


def _build_db_config(args: argparse.Namespace) -> DbConfig:
    config = DbConfig.from_env()
    return DbConfig(
        host=args.db_host or config.host,
        port=args.db_port or config.port,
        dbname=args.db_name or config.dbname,
        user=args.db_user or config.user,
        password=args.db_password or config.password,
    )


def _source_overrides_from_args(args: argparse.Namespace) -> dict[str, object]:
    source_name = _clean_source_name(args.source)
    if source_name == "chembl":
        return _non_null_overrides(
            {
                "target_chembl_id": args.target_chembl_id,
                "standard_type": args.standard_type,
                "standard_relation__in": args.relations,
            }
        )
    if source_name == "pubchem":
        return _non_null_overrides(
            {
                "target_gene_symbol": args.target_gene_symbol,
                "target_gene_id": args.target_gene_id,
                "activity_name_regex": args.activity_name_regex,
            }
        )
    raise AssertionError(f"Unhandled source '{source_name}'.")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest bioactivity data for a configured endpoint.")
    parser.add_argument("--endpoint-key", default="herg_ic50")
    parser.add_argument("--source", choices=sorted(SUPPORTED_SOURCES), required=True)

    parser.add_argument("--chembl-base-url", default=None)
    parser.add_argument("--target-chembl-id", default=None)
    parser.add_argument("--standard-type", default=None)
    parser.add_argument("--relations", default=None)
    parser.add_argument("--activity-page-size", type=int, default=None)
    parser.add_argument("--molecule-batch-size", type=int, default=None)

    parser.add_argument("--pubchem-base-url", default=None)
    parser.add_argument("--target-gene-symbol", default=None)
    parser.add_argument("--target-gene-id", default=None)
    parser.add_argument("--activity-name-regex", default=None)
    parser.add_argument("--cid-batch-size", type=int, default=None)

    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-records", type=int, default=None)
    parser.add_argument("--commit-every", type=int, default=500)
    parser.add_argument("--request-timeout-seconds", type=int, default=45)
    parser.add_argument("--http-retries", type=int, default=4)
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--errors-path", default=None)
    parser.add_argument("--stats-path", default=None)

    parser.add_argument("--db-host", default=None)
    parser.add_argument("--db-port", type=int, default=None)
    parser.add_argument("--db-name", default=None)
    parser.add_argument("--db-user", default=None)
    parser.add_argument("--db-password", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    http_config = HttpConfig(
        request_timeout_seconds=args.request_timeout_seconds,
        http_retries=args.http_retries,
    )
    run_config = RunConfig(
        dry_run=args.dry_run,
        max_records=args.max_records,
        commit_every=args.commit_every,
        fail_fast=args.fail_fast,
        errors_path=args.errors_path,
        stats_path=args.stats_path,
    )
    stats = run_endpoint_ingestion(
        endpoint_key=args.endpoint_key,
        source_name=args.source,
        db_config=_build_db_config(args),
        http_config=http_config,
        run_config=run_config,
        source_config_overrides=_source_overrides_from_args(args),
        chembl_base_url=args.chembl_base_url,
        pubchem_base_url=args.pubchem_base_url,
        activity_page_size=args.activity_page_size,
        molecule_batch_size=args.molecule_batch_size,
        cid_batch_size=args.cid_batch_size,
    )
    print_ingestion_summary(stats, source_name=args.source)
    return 1 if stats.failed > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
