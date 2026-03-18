from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Protocol

from .config import DbConfig, StructureRunConfig
from .db import (
    EnrichmentConflictError,
    apply_structure_enrichment,
    ensure_structure_enrichment_schema,
    get_conn,
    preview_structure_enrichment,
)
from .models import StructureEnrichmentRecord
from .normalize import clean_text
from .pipeline_common import JsonlLogger, log_jsonl, write_stats


DEFAULT_ENRICH_BATCH_SIZE = 250


class StructureSourceAdapter(Protocol):
    source_name: str

    def iter_raw_rows(self) -> Iterable[dict]:
        ...

    def enrich_batch(self, rows: list[dict]) -> list[dict]:
        ...

    def map_row(self, row: dict) -> StructureEnrichmentRecord:
        ...


@dataclass
class StructureEnrichmentStats:
    processed: int = 0
    attached: int = 0
    already_present: int = 0
    unmatched: int = 0
    conflict: int = 0
    skipped_invalid: int = 0
    failed: int = 0
    warnings: int = 0


def _enrich_batch_size(adapter: StructureSourceAdapter) -> int:
    return int(getattr(adapter, "enrich_batch_size", DEFAULT_ENRICH_BATCH_SIZE))


def _validate_structure_record(record: StructureEnrichmentRecord) -> None:
    if not clean_text(record.external_key):
        raise ValueError("Missing external_key.")

    has_match_key = bool(clean_text(record.match.standard_inchikey)) or bool(record.match.identifiers)
    if not has_match_key:
        raise ValueError("Structure enrichment record requires standard_inchikey or existing identifiers for matching.")

    structure = record.structure
    has_structure = any(
        clean_text(value)
        for value in (
            structure.canonical_smiles,
            structure.standard_inchi,
            structure.standard_inchikey,
            structure.connectivity_smiles,
        )
    )
    if not has_structure:
        raise ValueError("Structure enrichment record has no structure fields to attach.")

    if not clean_text(record.source_record.source_name):
        raise ValueError("source_name is required.")
    if not clean_text(record.source_record.source_record_key):
        raise ValueError("source_record_key is required.")
    if not clean_text(record.source_record.record_type):
        raise ValueError("record_type is required.")


def _row_status(row: dict) -> tuple[str, str]:
    status = clean_text(row.get("enrichment_status")) or clean_text(row.get("harvest_status"))
    reason = clean_text(row.get("enrichment_reason")) or clean_text(row.get("harvest_reason"))
    return status, reason


def run_structure_pipeline(
    adapter: StructureSourceAdapter,
    db_config: DbConfig,
    run_config: StructureRunConfig,
) -> StructureEnrichmentStats:
    stats = StructureEnrichmentStats()
    error_logger = JsonlLogger(run_config.errors_path)
    unmatched_logger = JsonlLogger(run_config.unmatched_path)
    conflict_logger = JsonlLogger(run_config.conflicts_path)
    enrich_batch_size = max(1, _enrich_batch_size(adapter))

    processed_since_commit = 0
    commit_every = max(1, run_config.commit_every)
    buffer: list[dict] = []

    def apply_outcome(record: StructureEnrichmentRecord, outcome) -> None:
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
                "Unmatched structure enrichment record.",
                asdict(record),
            )
        else:
            stats.warnings += 1
            log_jsonl(
                error_logger,
                adapter.source_name,
                record.external_key,
                f"Unexpected structure enrichment outcome '{outcome.status}'.",
                asdict(record),
            )

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
                    row_reason or "Unmatched structure enrichment record.",
                    row,
                )
                continue

            if row_status == "conflict":
                stats.conflict += 1
                log_jsonl(
                    conflict_logger,
                    adapter.source_name,
                    raw_external_key,
                    row_reason or "Conflicting structure enrichment record.",
                    row,
                )
                if run_config.fail_fast:
                    raise EnrichmentConflictError(row_reason or "Conflicting structure enrichment record.")
                continue

            if row_status and row_status != "candidate":
                stats.failed += 1
                log_jsonl(
                    error_logger,
                    adapter.source_name,
                    raw_external_key,
                    row_reason or f"Unexpected structure enrichment status '{row_status}'.",
                    row,
                )
                if run_config.fail_fast:
                    raise ValueError(row_reason or f"Unexpected structure enrichment status '{row_status}'.")
                continue

            try:
                record = adapter.map_row(row)
                _validate_structure_record(record)
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
                    outcome = preview_structure_enrichment(cur, record)
                else:
                    cur.execute("SAVEPOINT structure_row")
                    try:
                        outcome = apply_structure_enrichment(cur, record)
                        cur.execute("RELEASE SAVEPOINT structure_row")
                        processed_since_commit += 1
                    except Exception:
                        cur.execute("ROLLBACK TO SAVEPOINT structure_row")
                        cur.execute("RELEASE SAVEPOINT structure_row")
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
                ensure_structure_enrichment_schema(cur)
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

    write_stats(run_config.stats_path, asdict(stats))
    return stats
