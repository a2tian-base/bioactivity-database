from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Protocol

from .config import DbConfig, IdentifierRunConfig
from .db import (
    EnrichmentConflictError,
    apply_identifier_enrichment,
    ensure_identifier_enrichment_schema,
    get_conn,
    preview_identifier_enrichment,
)
from .models import IdentifierEnrichmentRecord
from .normalize import clean_text
from .pipeline_common import JsonlLogger, duration_seconds, log_jsonl, now_utc_iso, write_stats


DEFAULT_ENRICH_BATCH_SIZE = 250


class IdentifierSourceAdapter(Protocol):
    source_name: str

    def iter_raw_rows(self) -> Iterable[dict]:
        ...

    def enrich_batch(self, rows: list[dict]) -> list[dict]:
        ...

    def map_row(self, row: dict) -> IdentifierEnrichmentRecord:
        ...


@dataclass
class IdentifierEnrichmentStats:
    processed: int = 0
    attached: int = 0
    already_present: int = 0
    unmatched: int = 0
    conflict: int = 0
    skipped_invalid: int = 0
    failed: int = 0
    warnings: int = 0
    created_compounds: int = 0
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0


def _enrich_batch_size(adapter: IdentifierSourceAdapter) -> int:
    return int(getattr(adapter, "enrich_batch_size", DEFAULT_ENRICH_BATCH_SIZE))


def _validate_identifier_record(record: IdentifierEnrichmentRecord) -> None:
    if not clean_text(record.external_key):
        raise ValueError("Missing external_key.")

    has_match_key = bool(clean_text(record.match.standard_inchikey)) or bool(record.match.identifiers)
    if not has_match_key:
        raise ValueError("Enrichment record requires standard_inchikey or existing identifiers for matching.")

    if not record.identifiers_to_add and not record.names_to_add:
        raise ValueError("Enrichment record has nothing to attach.")

    if record.source_record is not None:
        if not clean_text(record.source_record.source_name):
            raise ValueError("source_name is required when source_record is provided.")
        if not clean_text(record.source_record.source_record_key):
            raise ValueError("source_record_key is required when source_record is provided.")
        if not clean_text(record.source_record.record_type):
            raise ValueError("record_type is required when source_record is provided.")


def _row_status(row: dict) -> tuple[str, str]:
    status = clean_text(row.get("enrichment_status")) or clean_text(row.get("harvest_status"))
    reason = clean_text(row.get("enrichment_reason")) or clean_text(row.get("harvest_reason"))
    return status, reason


def run_identifier_pipeline(
    adapter: IdentifierSourceAdapter,
    db_config: DbConfig,
    run_config: IdentifierRunConfig,
) -> IdentifierEnrichmentStats:
    stats = IdentifierEnrichmentStats(started_at=now_utc_iso())
    error_logger = JsonlLogger(run_config.errors_path)
    unmatched_logger = JsonlLogger(run_config.unmatched_path)
    conflict_logger = JsonlLogger(run_config.conflicts_path)
    enrich_batch_size = max(1, _enrich_batch_size(adapter))

    processed_since_commit = 0
    commit_every = max(1, run_config.commit_every)
    buffer: list[dict] = []

    def apply_outcome(record: IdentifierEnrichmentRecord, outcome) -> None:
        if outcome.status == "attached":
            stats.attached += 1
        elif outcome.status == "already_present":
            stats.already_present += 1
        elif outcome.status == "unmatched":
            stats.unmatched += 1
            log_jsonl(
                unmatched_logger,
                adapter.source_name,
                record.external_key,
                "Unmatched compound enrichment record.",
                asdict(record),
            )
        else:
            stats.warnings += 1
            log_jsonl(
                error_logger,
                adapter.source_name,
                record.external_key,
                f"Unexpected enrichment outcome '{outcome.status}'.",
                asdict(record),
            )
        if outcome.created_compound:
            stats.created_compounds += 1

    def process_rows(rows: list[dict], cur) -> None:
        nonlocal processed_since_commit
        enriched_rows = adapter.enrich_batch(rows)
        if len(enriched_rows) != len(rows):
            stats.warnings += 1

        for row in enriched_rows:
            raw_external_key = clean_text(row.get("external_key", ""))
            row_status, row_reason = _row_status(row)

            if row_status == "unmatched":
                stats.unmatched += 1
                log_jsonl(
                    unmatched_logger,
                    adapter.source_name,
                    raw_external_key,
                    row_reason or "Unmatched compound enrichment record.",
                    row,
                )
                continue

            if row_status == "conflict":
                stats.conflict += 1
                log_jsonl(
                    conflict_logger,
                    adapter.source_name,
                    raw_external_key,
                    row_reason or "Conflicting compound enrichment record.",
                    row,
                )
                if run_config.fail_fast:
                    raise EnrichmentConflictError(row_reason or "Conflicting compound enrichment record.")
                continue

            if row_status and row_status != "candidate":
                stats.failed += 1
                log_jsonl(
                    error_logger,
                    adapter.source_name,
                    raw_external_key,
                    row_reason or f"Unexpected enrichment status '{row_status}'.",
                    row,
                )
                if run_config.fail_fast:
                    raise ValueError(row_reason or f"Unexpected enrichment status '{row_status}'.")
                continue

            try:
                record = adapter.map_row(row)
                _validate_identifier_record(record)
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

            try:
                if run_config.dry_run:
                    outcome = preview_identifier_enrichment(
                        cur,
                        record,
                        create_missing_compounds=run_config.create_missing_compounds,
                    )
                else:
                    cur.execute("SAVEPOINT enrich_row")
                    try:
                        outcome = apply_identifier_enrichment(
                            cur,
                            record,
                            create_missing_compounds=run_config.create_missing_compounds,
                        )
                        cur.execute("RELEASE SAVEPOINT enrich_row")
                        processed_since_commit += 1
                    except Exception:
                        cur.execute("ROLLBACK TO SAVEPOINT enrich_row")
                        cur.execute("RELEASE SAVEPOINT enrich_row")
                        raise
            except EnrichmentConflictError as exc:
                stats.conflict += 1
                log_jsonl(conflict_logger, adapter.source_name, record.external_key, str(exc), asdict(record))
                if run_config.fail_fast:
                    raise
                continue
            except Exception as exc:
                stats.failed += 1
                log_jsonl(error_logger, adapter.source_name, record.external_key, str(exc), asdict(record))
                if run_config.fail_fast:
                    raise
                continue

            apply_outcome(record, outcome)

            if not run_config.dry_run and processed_since_commit >= commit_every:
                cur.connection.commit()
                processed_since_commit = 0

    try:
        with get_conn(db_config=db_config) as conn:
            with conn.cursor() as cur:
                ensure_identifier_enrichment_schema(cur)
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

                if not run_config.dry_run and processed_since_commit > 0:
                    conn.commit()
    finally:
        error_logger.close()
        unmatched_logger.close()
        conflict_logger.close()

    stats.finished_at = now_utc_iso()
    stats.duration_seconds = duration_seconds(stats.started_at, stats.finished_at)
    write_stats(run_config.stats_path, asdict(stats))
    return stats
