#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from pathlib import Path
import sys
from typing import Any, Callable, Iterable

from ..config import DbConfig, HttpConfig
from ..http import post_json
from ..models import CompoundMatchInput, IdentifierEnrichmentRecord, IdentifierInput, SourceRecordInput
from ..normalize import build_identifier_inputs, clean_text
from ..pipeline_common import JsonlLogger, duration_seconds, log_jsonl, now_utc_iso, write_stats
from ..read_db import fetch_identifier_enrichment_candidates


UNICHEM_BASE_URL = "https://www.ebi.ac.uk/unichem/api/v1"
TARGET_SOURCE_IDS = {
    "chembl_id": 1,
    "pubchem_cid": 22,
    "unii": 14,
}
OUTPUT_FIELDNAMES = [
    "compound_id",
    "preferred_name",
    "match_inchikey",
    "match_chembl_id",
    "match_pubchem_cid",
    "match_unii",
    "add_namespace",
    "add_value",
    "is_primary",
    "source_record_key",
    "lookup_type",
    "lookup_value",
]
DEFAULT_BATCH_SIZE = 100


@dataclass
class HarvestStats:
    processed: int = 0
    written: int = 0
    unmatched: int = 0
    conflict: int = 0
    failed: int = 0
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0


class UniChemIdentifierAdapter:
    source_name = "unichem"

    def __init__(
        self,
        http_config: HttpConfig,
        target_namespace: str,
        base_url: str = UNICHEM_BASE_URL,
        limit: int | None = None,
        enrich_batch_size: int = DEFAULT_BATCH_SIZE,
        db_config: DbConfig | None = None,
        progress_logger: Callable[[str], None] | None = None,
    ) -> None:
        if target_namespace not in TARGET_SOURCE_IDS:
            raise ValueError(f"Unsupported target_namespace '{target_namespace}'.")
        self.http_config = http_config
        self.target_namespace = target_namespace
        self.target_source_id = TARGET_SOURCE_IDS[target_namespace]
        self.base_url = base_url.rstrip("/")
        self.limit = limit
        self.enrich_batch_size = max(1, enrich_batch_size)
        self.db_config = db_config
        self.progress_logger = progress_logger
        self.last_candidate_count = 0
        self._candidate_rows: list[dict[str, Any]] | None = None
        self._progress_processed = 0

    def load_candidates(self) -> int:
        self._candidate_rows = fetch_identifier_enrichment_candidates(
            self.target_namespace,
            limit=self.limit,
            db_config=self.db_config,
        )
        self.last_candidate_count = len(self._candidate_rows)
        self._progress_processed = 0
        return self.last_candidate_count

    def iter_raw_rows(self) -> Iterable[dict[str, Any]]:
        if self._candidate_rows is None:
            self.load_candidates()

        self._progress_processed = 0
        for row in self._candidate_rows or []:
            enriched = dict(row)
            enriched["external_key"] = f"compound:{row['compound_id']}|target:{self.target_namespace}"
            yield enriched

    def enrich_batch(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        enriched_rows: list[dict[str, Any]] = []
        for row in rows:
            enriched = dict(row)
            try:
                lookup = self._choose_lookup(enriched)
                if lookup is None:
                    enriched["harvest_status"] = "unmatched"
                    enriched["harvest_reason"] = "No exact lookup key available."
                    enriched_rows.append(enriched)
                    continue

                lookup_type, lookup_value, payload = lookup
                response = post_json(f"{self.base_url}/compounds", payload, self.http_config, label="UniChem")
                target_values = self._extract_target_values(response)

                enriched["lookup_type"] = lookup_type
                enriched["lookup_value"] = lookup_value
                enriched["unichem_response"] = response
                enriched["target_values"] = target_values

                if not target_values:
                    enriched["harvest_status"] = "unmatched"
                    enriched["harvest_reason"] = f"UniChem returned no {self.target_namespace} mapping."
                elif len(target_values) > 1:
                    enriched["harvest_status"] = "conflict"
                    enriched["harvest_reason"] = (
                        f"UniChem returned multiple {self.target_namespace} mappings: {', '.join(target_values)}."
                    )
                else:
                    enriched["harvest_status"] = "candidate"
                    enriched["harvest_reason"] = ""
            except Exception as exc:
                enriched["harvest_status"] = "error"
                enriched["harvest_reason"] = str(exc)

            enriched_rows.append(enriched)

        self._progress_processed += len(rows)
        if self.progress_logger is not None and self.last_candidate_count > 0:
            percent = (self._progress_processed / self.last_candidate_count) * 100
            self.progress_logger(
                f"[{self.target_namespace}] Progress: "
                f"{self._progress_processed}/{self.last_candidate_count} candidate rows "
                f"({percent:.1f}%)"
            )

        return enriched_rows

    def map_row(self, row: dict[str, Any]) -> IdentifierEnrichmentRecord:
        status = clean_text(row.get("harvest_status"))
        if status != "candidate":
            raise ValueError(f"Cannot map row with harvest_status '{status or 'unknown'}'.")

        target_values = row.get("target_values") or []
        if len(target_values) != 1:
            raise ValueError("Candidate row requires exactly one target identifier.")

        lookup_type = clean_text(row.get("lookup_type"))
        lookup_value = clean_text(row.get("lookup_value"))
        compound_id = row.get("compound_id")
        match = CompoundMatchInput(
            standard_inchikey=clean_text(row.get("standard_inchikey")),
            identifiers=build_identifier_inputs(
                {
                    "chembl_id": row.get("chembl_id"),
                    "pubchem_cid": row.get("pubchem_cid"),
                    "unii": row.get("unii"),
                }
            ),
        )
        identifier_to_add = IdentifierInput(
            namespace=self.target_namespace,
            value=str(target_values[0]),
            is_primary=True,
        )
        source_record_key = (
            f"compound:{compound_id}|lookup:{lookup_type}:{lookup_value}|target:{self.target_namespace}"
        )

        return IdentifierEnrichmentRecord(
            external_key=f"compound:{compound_id}|add:{self.target_namespace}",
            match=match,
            identifiers_to_add=[identifier_to_add],
            source_record=SourceRecordInput(
                source_name=self.source_name,
                source_record_key=source_record_key,
                record_type="identifier_enrichment",
                raw_payload={
                    "seed_compound_id": compound_id,
                    "seed_row": {
                        "compound_id": row.get("compound_id"),
                        "standard_inchikey": row.get("standard_inchikey"),
                        "chembl_id": row.get("chembl_id"),
                        "pubchem_cid": row.get("pubchem_cid"),
                        "unii": row.get("unii"),
                        "preferred_name": row.get("preferred_name"),
                    },
                    "lookup": {
                        "type": lookup_type,
                        "value": lookup_value,
                    },
                    "target_namespace": self.target_namespace,
                    "unichem_response": row.get("unichem_response") or {},
                },
            ),
        )

    def record_to_csv_row(
        self,
        record: IdentifierEnrichmentRecord,
        row: dict[str, Any],
    ) -> dict[str, str]:
        identifier = record.identifiers_to_add[0]
        source_record_key = record.source_record.source_record_key if record.source_record else ""
        return {
            "compound_id": str(row.get("compound_id") or ""),
            "preferred_name": clean_text(row.get("preferred_name")),
            "match_inchikey": clean_text(row.get("standard_inchikey")),
            "match_chembl_id": clean_text(row.get("chembl_id")),
            "match_pubchem_cid": clean_text(row.get("pubchem_cid")),
            "match_unii": clean_text(row.get("unii")),
            "add_namespace": identifier.namespace,
            "add_value": identifier.value,
            "is_primary": "true" if identifier.is_primary else "false",
            "source_record_key": source_record_key,
            "lookup_type": clean_text(row.get("lookup_type")),
            "lookup_value": clean_text(row.get("lookup_value")),
        }

    def _choose_lookup(self, row: dict[str, Any]) -> tuple[str, str, dict[str, object]] | None:
        standard_inchikey = clean_text(row.get("standard_inchikey"))
        if standard_inchikey:
            return (
                "inchikey",
                standard_inchikey,
                {
                    "compound": standard_inchikey,
                    "type": "inchikey",
                },
            )

        for namespace in ("chembl_id", "pubchem_cid", "unii"):
            value = clean_text(row.get(namespace))
            if not value:
                continue
            return (
                namespace,
                value,
                {
                    "compound": value,
                    "type": "sourceID",
                    "sourceID": TARGET_SOURCE_IDS[namespace],
                },
            )

        return None

    def _extract_target_values(self, response: dict[str, Any]) -> list[str]:
        values: list[str] = []
        seen: set[str] = set()

        for compound in response.get("compounds") or []:
            for source in compound.get("sources") or []:
                if int(source.get("id") or 0) != self.target_source_id:
                    continue
                candidate = clean_text(source.get("compoundId"))
                if not candidate:
                    continue
                normalized = candidate.casefold()
                if normalized in seen:
                    continue
                seen.add(normalized)
                values.append(candidate)

        return values


def _chunked(rows: Iterable[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build identifier-enrichment CSV candidates using UniChem.")
    parser.add_argument("output_csv")
    parser.add_argument("--target-namespace", choices=sorted(TARGET_SOURCE_IDS), required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--unichem-base-url", default=UNICHEM_BASE_URL)
    parser.add_argument("--request-timeout-seconds", type=int, default=45)
    parser.add_argument("--http-retries", type=int, default=4)
    parser.add_argument("--errors-path", default=None)
    parser.add_argument("--unmatched-path", default=None)
    parser.add_argument("--conflicts-path", default=None)
    parser.add_argument("--stats-path", default=None)
    return parser.parse_args()


def _log(message: str) -> None:
    print(message, flush=True)


def main() -> int:
    args = _parse_args()
    output_csv = Path(args.output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)

    adapter = UniChemIdentifierAdapter(
        http_config=HttpConfig(
            request_timeout_seconds=args.request_timeout_seconds,
            http_retries=args.http_retries,
        ),
        target_namespace=args.target_namespace,
        base_url=args.unichem_base_url,
        limit=args.limit,
        enrich_batch_size=args.batch_size,
        db_config=DbConfig.from_env(),
        progress_logger=_log,
    )

    _log("Scanning database for UniChem harvest candidates...")
    candidate_count = adapter.load_candidates()
    _log(f"Target namespace: {args.target_namespace}")
    _log(f"Candidate rows found: {candidate_count}")
    _log(f"Output CSV: {output_csv}")
    _log("")
    _log(f"Starting UniChem candidate harvest for {args.target_namespace}...")

    stats = HarvestStats(started_at=now_utc_iso())
    error_logger = JsonlLogger(args.errors_path)
    unmatched_logger = JsonlLogger(args.unmatched_path)
    conflict_logger = JsonlLogger(args.conflicts_path)

    try:
        with output_csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDNAMES)
            writer.writeheader()

            for raw_batch in _chunked(adapter.iter_raw_rows(), max(1, args.batch_size)):
                enriched_batch = adapter.enrich_batch(raw_batch)
                for row in enriched_batch:
                    stats.processed += 1
                    status = clean_text(row.get("harvest_status"))

                    if status == "candidate":
                        try:
                            record = adapter.map_row(row)
                        except Exception as exc:
                            stats.failed += 1
                            log_jsonl(error_logger, adapter.source_name, clean_text(row.get("external_key")), str(exc), row)
                            continue
                        writer.writerow(adapter.record_to_csv_row(record, row))
                        stats.written += 1
                        continue

                    if status == "unmatched":
                        stats.unmatched += 1
                        log_jsonl(
                            unmatched_logger,
                            adapter.source_name,
                            clean_text(row.get("external_key")),
                            clean_text(row.get("harvest_reason")) or "No UniChem match found.",
                            row,
                        )
                        continue

                    if status == "conflict":
                        stats.conflict += 1
                        log_jsonl(
                            conflict_logger,
                            adapter.source_name,
                            clean_text(row.get("external_key")),
                            clean_text(row.get("harvest_reason")) or "UniChem returned multiple target identifiers.",
                            row,
                        )
                        continue

                    stats.failed += 1
                    log_jsonl(
                        error_logger,
                        adapter.source_name,
                        clean_text(row.get("external_key")),
                        f"Unexpected harvest_status '{status or 'unknown'}'.",
                        row,
                    )
    finally:
        error_logger.close()
        unmatched_logger.close()
        conflict_logger.close()

    stats.finished_at = now_utc_iso()
    stats.duration_seconds = duration_seconds(stats.started_at, stats.finished_at)
    write_stats(args.stats_path, asdict(stats))

    _log("")
    _log("UniChem harvest summary")
    _log("-----------------------")
    _log(f"Target namespace: {args.target_namespace}")
    _log(f"Candidate rows found: {adapter.last_candidate_count}")
    _log(f"Processed candidates: {stats.processed}")
    _log(f"Written CSV rows: {stats.written}")
    _log(f"Unmatched: {stats.unmatched}")
    _log(f"Conflicts: {stats.conflict}")
    _log(f"Failed: {stats.failed}")
    _log(f"Output CSV: {output_csv}")
    if adapter.last_candidate_count == 0:
        _log("No candidate compounds matched the current target namespace filter.")

    return 1 if stats.failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
