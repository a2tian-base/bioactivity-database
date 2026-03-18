#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys

from ..config import DbConfig, IdentifierRunConfig
from ..identifier_pipeline import run_identifier_pipeline
from ..models import CompoundMatchInput, IdentifierEnrichmentRecord, SourceRecordInput
from ..normalize import build_identifier_inputs, clean_text, parse_bool


class IdentifiersCsvAdapter:
    source_name = "identifier_csv"

    def __init__(
        self,
        csv_path: Path,
        source_name: str = "identifier_csv",
    ) -> None:
        self.csv_path = csv_path
        self.source_name = source_name
        self.enrich_batch_size = 500

    def iter_raw_rows(self):
        with self.csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for line_number, row in enumerate(reader, start=2):
                source_record_key = clean_text(row.get("source_record_key")) or f"row:{line_number}"
                yield {
                    **row,
                    "_line_number": line_number,
                    "external_key": source_record_key,
                }

    def enrich_batch(self, rows: list[dict]) -> list[dict]:
        return rows

    def map_row(self, row: dict) -> IdentifierEnrichmentRecord:
        line_number = int(row.get("_line_number") or 0)

        if clean_text(row.get("match_name")) or clean_text(row.get("match_preferred_name")):
            raise ValueError("Name-based matching is not supported for identifier enrichment.")

        match_inchikey = clean_text(row.get("match_inchikey")) or clean_text(row.get("match_standard_inchikey"))
        match_identifier_map: dict[str, str] = {}
        for key, value in row.items():
            if not key.startswith("match_"):
                continue
            if key in {"match_inchikey", "match_standard_inchikey", "match_name", "match_preferred_name"}:
                continue
            cleaned_value = clean_text(value)
            if not cleaned_value:
                continue
            match_identifier_map[key[len("match_") :]] = cleaned_value

        add_namespace = clean_text(row.get("add_namespace"))
        add_value = clean_text(row.get("add_value"))
        is_primary = parse_bool(row.get("is_primary"))
        identifiers_to_add = build_identifier_inputs(
            {add_namespace: add_value} if add_namespace else {},
            add_namespace if is_primary else None,
        )

        match = CompoundMatchInput(
            standard_inchikey=match_inchikey,
            identifiers=build_identifier_inputs(match_identifier_map),
        )

        source_record_key = clean_text(row.get("source_record_key")) or f"row:{line_number}"
        source_record = SourceRecordInput(
            source_name=self.source_name,
            source_record_key=source_record_key,
            record_type="identifier_enrichment",
            raw_payload={
                "csv_row": {
                    key: value
                    for key, value in row.items()
                    if not key.startswith("_") and key != "external_key"
                },
                "line_number": line_number,
                "csv_path": self.csv_path.name,
            },
        )

        return IdentifierEnrichmentRecord(
            external_key=source_record_key,
            match=match,
            identifiers_to_add=identifiers_to_add,
            source_record=source_record,
        )


def _build_db_config(args: argparse.Namespace) -> DbConfig:
    config = DbConfig.from_env()
    return DbConfig(
        host=args.db_host or config.host,
        port=args.db_port or config.port,
        dbname=args.db_name or config.dbname,
        user=args.db_user or config.user,
        password=args.db_password or config.password,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Attach identifiers to existing compounds from a curated CSV.")
    parser.add_argument("csv_path")
    parser.add_argument("--source-name", default="identifier_csv")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-records", type=int, default=None)
    parser.add_argument("--commit-every", type=int, default=500)
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--errors-path", default=None)
    parser.add_argument("--stats-path", default=None)
    parser.add_argument("--unmatched-path", default=None)
    parser.add_argument("--conflicts-path", default=None)
    parser.add_argument("--create-missing-compounds", action="store_true")
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
    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        raise SystemExit(f"CSV file not found: {csv_path}")

    run_config = IdentifierRunConfig(
        dry_run=args.dry_run,
        max_records=args.max_records,
        commit_every=args.commit_every,
        fail_fast=args.fail_fast,
        errors_path=args.errors_path,
        stats_path=args.stats_path,
        unmatched_path=args.unmatched_path,
        conflicts_path=args.conflicts_path,
        create_missing_compounds=args.create_missing_compounds,
    )
    db_config = _build_db_config(args)
    adapter = IdentifiersCsvAdapter(
        csv_path=csv_path,
        source_name=args.source_name,
    )

    stats = run_identifier_pipeline(adapter, db_config, run_config)

    _log("")
    _log("Identifier enrichment summary")
    _log("-----------------------------")
    _log(f"Source: {adapter.source_name}")
    _log(f"Processed rows: {stats.processed}")
    _log(f"Attached: {stats.attached}")
    _log(f"Already present: {stats.already_present}")
    _log(f"Unmatched: {stats.unmatched}")
    _log(f"Conflicts: {stats.conflict}")
    _log(f"Skipped invalid: {stats.skipped_invalid}")
    _log(f"Failed rows: {stats.failed}")

    return 1 if stats.failed > 0 or stats.conflict > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
