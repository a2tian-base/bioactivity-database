from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Protocol

from bioactivity.db import upsert_bioactivity_result
from bioactivity.endpoints import load_endpoint
from bioactivity.models import MeasurementInput, measurement_from_ic50
from bioactivity.runs import finish_ingestion_run, start_ingestion_run
from .config import DbConfig, RunConfig
from .db import (
    ensure_measurement_ingest_schema,
    get_conn,
    upsert_compound,
    upsert_ic50_result,
    upsert_source_record,
)
from .models import StagedRecord
from .normalize import ALLOWED_IC50_UNITS, ALLOWED_QUALIFIERS, clean_text
from .pipeline_common import JsonlLogger, duration_seconds, log_jsonl, now_utc_iso, write_stats


DEFAULT_ENRICH_BATCH_SIZE = 250
DEFAULT_ENDPOINT_KEY = "herg_ic50"


def log(message: str) -> None:
    print(message, flush=True)


class SourceAdapter(Protocol):
    source_name: str

    def iter_raw_rows(self) -> Iterable[dict]:
        ...

    def enrich_batch(self, rows: list[dict]) -> list[dict]:
        ...

    def map_row(self, row: dict) -> StagedRecord:
        ...


@dataclass
class IngestionStats:
    processed: int = 0
    stored: int = 0
    updated: int = 0
    skipped_invalid: int = 0
    failed: int = 0
    warnings: int = 0
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0


def _validate_staged_record(record: StagedRecord) -> None:
    if not clean_text(record.external_key):
        raise ValueError("Missing external_key.")

    has_identifier = bool(record.compound.identifiers)
    has_inchikey = bool(clean_text(record.compound.standard_inchikey))
    if not has_identifier and not has_inchikey:
        raise ValueError("Compound requires at least one identifier or standard_inchikey.")

    source_name = clean_text(record.source_record.source_name)
    source_key = clean_text(record.source_record.source_record_key)
    record_type = clean_text(record.source_record.record_type)
    if not source_name or not source_key or not record_type:
        raise ValueError("source_name, source_record_key, and record_type are required.")

    if record.measurement.ic50_value <= 0:
        raise ValueError("ic50_value must be > 0.")
    if record.measurement.ic50_unit not in ALLOWED_IC50_UNITS:
        raise ValueError("ic50_unit is invalid.")
    if record.measurement.qualifier not in ALLOWED_QUALIFIERS:
        raise ValueError("qualifier is invalid.")


def _enrich_batch_size(adapter: SourceAdapter) -> int:
    return int(getattr(adapter, "enrich_batch_size", DEFAULT_ENRICH_BATCH_SIZE))


def _adapter_query_config(adapter: SourceAdapter) -> dict[str, Any]:
    config = getattr(adapter, "effective_config", None)
    if isinstance(config, Mapping):
        return dict(config)
    return {}


def _run_counters(stats: IngestionStats) -> dict[str, int]:
    return {
        "rows_seen": stats.processed,
        "rows_inserted": stats.stored,
        "rows_updated": stats.updated,
        "rows_skipped": stats.skipped_invalid,
        "rows_failed": stats.failed,
    }


def _run_status(stats: IngestionStats, *, uncaught_error: bool = False) -> str:
    if uncaught_error:
        return "partial" if stats.stored > 0 else "failed"
    if stats.failed > 0:
        return "partial" if stats.stored > 0 else "failed"
    if stats.skipped_invalid > 0:
        return "partial"
    return "succeeded"


def _measurement_input_from_staged_record(
    staged: StagedRecord,
    ic50_result: dict[str, Any],
    source_name: str,
) -> MeasurementInput:
    return measurement_from_ic50(
        result_key=clean_text(staged.external_key) or clean_text(staged.source_record.source_record_key),
        ic50_value=staged.measurement.ic50_value,
        ic50_unit=staged.measurement.ic50_unit,
        qualifier=staged.measurement.qualifier,
        ic50_um=ic50_result.get("ic50_um"),
        pic50=ic50_result.get("pic50"),
        pic50_qualifier=ic50_result.get("pic50_qualifier"),
        quality_flags={"source": clean_text(source_name)},
    )


def _measurement_input_for_adapter(
    adapter: SourceAdapter,
    staged: StagedRecord,
    ic50_result: dict[str, Any],
) -> MeasurementInput:
    mapper = getattr(adapter, "measurement_input_from_record", None)
    if callable(mapper):
        return mapper(staged, ic50_result)
    return _measurement_input_from_staged_record(staged, ic50_result, adapter.source_name)


def run_pipeline(
    adapter: SourceAdapter,
    db_config: DbConfig,
    run_config: RunConfig,
    *,
    endpoint_key: str = DEFAULT_ENDPOINT_KEY,
) -> IngestionStats:
    stats = IngestionStats()
    stats.started_at = now_utc_iso()
    error_logger = JsonlLogger(run_config.errors_path)
    enrich_batch_size = max(1, _enrich_batch_size(adapter))

    processed_since_commit = 0
    commit_every = max(1, run_config.commit_every)
    buffer: list[dict] = []
    endpoint_id: int | None = None
    ingestion_run_id: int | None = None

    def process_rows(rows: list[dict], cur) -> None:
        nonlocal processed_since_commit
        enriched_rows = adapter.enrich_batch(rows)
        if len(enriched_rows) != len(rows):
            stats.warnings += 1
        for row in enriched_rows:
            raw_external_key = clean_text(row.get("external_key", ""))
            try:
                staged = adapter.map_row(row)
                _validate_staged_record(staged)
            except ValueError as exc:
                stats.skipped_invalid += 1
                log_jsonl(error_logger, adapter.source_name, raw_external_key, str(exc), row)
                if run_config.fail_fast:
                    raise
                continue
            except Exception as exc:
                stats.failed += 1
                log_jsonl(error_logger, adapter.source_name, raw_external_key, str(exc), row)
                if run_config.fail_fast:
                    raise
                continue

            if run_config.dry_run:
                stats.stored += 1
                continue

            cur.execute("SAVEPOINT ingest_row")
            try:
                compound_id = upsert_compound(cur, staged.compound)
                source_record_id = upsert_source_record(cur, staged.source_record)
                ic50_result = upsert_ic50_result(cur, compound_id, source_record_id, staged.measurement)
                if endpoint_id is not None:
                    upsert_bioactivity_result(
                        cur,
                        endpoint_id=endpoint_id,
                        compound_id=compound_id,
                        source_record_id=source_record_id,
                        ingestion_run_id=ingestion_run_id,
                        measurement=_measurement_input_for_adapter(adapter, staged, ic50_result),
                    )
                cur.execute("RELEASE SAVEPOINT ingest_row")
                stats.stored += 1
                processed_since_commit += 1
            except Exception as exc:
                cur.execute("ROLLBACK TO SAVEPOINT ingest_row")
                cur.execute("RELEASE SAVEPOINT ingest_row")
                stats.failed += 1
                log_jsonl(error_logger, adapter.source_name, staged.external_key, str(exc), asdict(staged))
                if run_config.fail_fast:
                    raise

            if processed_since_commit >= commit_every:
                cur.connection.commit()
                processed_since_commit = 0

    try:
        with get_conn(
            host=db_config.host,
            port=db_config.port,
            dbname=db_config.dbname,
            user=db_config.user,
            password=db_config.password,
        ) as conn:
            with conn.cursor() as cur:
                ensure_measurement_ingest_schema(cur)
                if not run_config.dry_run:
                    endpoint = load_endpoint(cur, endpoint_key)
                    endpoint_id = endpoint.endpoint_id
                    ingestion_run_id = start_ingestion_run(
                        cur,
                        endpoint_id=endpoint.endpoint_id,
                        source_name=adapter.source_name,
                        source_release=getattr(adapter, "release", None),
                        query_config=_adapter_query_config(adapter),
                    )

                try:
                    for raw_row in adapter.iter_raw_rows():
                        if run_config.max_records is not None and stats.processed >= run_config.max_records:
                            break
                        stats.processed += 1
                        buffer.append(raw_row)
                        if len(buffer) >= enrich_batch_size:
                            process_rows(buffer, cur)
                            buffer = []

                    if buffer:
                        process_rows(buffer, cur)

                    if not run_config.dry_run and ingestion_run_id is not None:
                        finish_ingestion_run(
                            cur,
                            ingestion_run_id=ingestion_run_id,
                            status=_run_status(stats),
                            counters=_run_counters(stats),
                            qc_summary={"warnings": stats.warnings},
                            error_summary={
                                "skipped_invalid": stats.skipped_invalid,
                                "failed": stats.failed,
                            },
                        )

                    if not run_config.dry_run and (processed_since_commit > 0 or ingestion_run_id is not None):
                        conn.commit()
                except Exception:
                    if not run_config.dry_run and ingestion_run_id is not None:
                        stats.failed += 1
                        finish_ingestion_run(
                            cur,
                            ingestion_run_id=ingestion_run_id,
                            status=_run_status(stats, uncaught_error=True),
                            counters=_run_counters(stats),
                            qc_summary={"warnings": stats.warnings},
                            error_summary={
                                "skipped_invalid": stats.skipped_invalid,
                                "failed": stats.failed,
                            },
                        )
                        conn.commit()
                    raise
    finally:
        error_logger.close()

    stats.finished_at = now_utc_iso()
    stats.duration_seconds = duration_seconds(stats.started_at, stats.finished_at)
    write_stats(
        run_config.stats_path,
        {
            "processed": stats.processed,
            "stored": stats.stored,
            "updated": stats.updated,
            "skipped_invalid": stats.skipped_invalid,
            "failed": stats.failed,
            "warnings": stats.warnings,
            "started_at": stats.started_at,
            "finished_at": stats.finished_at,
            "duration_seconds": stats.duration_seconds,
        },
    )

    return stats
