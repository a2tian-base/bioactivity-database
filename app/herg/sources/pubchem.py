#!/usr/bin/env python3
"""
PubChem source adapter for hERG IC50 ingestion.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Dict, Iterable, List, Sequence

from ..config import DbConfig, HttpConfig, RunConfig
from ..http import get_csv_rows, get_json
from ..models import CompoundInput, Ic50Input, SourceRecordInput, StagedRecord
from ..normalize import (
    build_identifier_inputs,
    build_name_inputs,
    clean_text,
    normalize_ic50_unit,
    parse_positive_float,
    parse_positive_int,
)
from ..pipeline import run_pipeline


PUBCHEM_BASE_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
DEFAULT_CONCISE_PATH = "/assay/target/genesymbol/{gene_symbol}/concise/CSV"


def _chunked(values: Sequence[int], size: int) -> Iterable[Sequence[int]]:
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


def _log(message: str) -> None:
    print(message, flush=True)


class PubChemAdapter:
    source_name = "pubchem"

    def __init__(
        self,
        http_config: HttpConfig,
        base_url: str,
        target_gene_symbol: str,
        target_gene_id: str,
        activity_name_regex: str,
        cid_batch_size: int,
    ) -> None:
        self.http_config = http_config
        self.base_url = base_url.rstrip("/")
        self.target_gene_symbol = target_gene_symbol
        self.target_gene_id = target_gene_id
        self.activity_name_pattern = re.compile(activity_name_regex)
        self.cid_batch_size = cid_batch_size
        self.enrich_batch_size = max(1, cid_batch_size)

    def iter_raw_rows(self) -> Iterable[dict]:
        concise_url = self.base_url + DEFAULT_CONCISE_PATH.format(gene_symbol=self.target_gene_symbol)
        scanned = 0
        kept = 0

        for row in get_csv_rows(concise_url, self.http_config, label="PubChem"):
            scanned += 1
            gene_id = clean_text(row.get("Target GeneID"))
            if gene_id != self.target_gene_id:
                continue

            activity_name = clean_text(row.get("Activity Name"))
            if not self.activity_name_pattern.search(activity_name):
                continue

            value_text = clean_text(row.get("Activity Value [uM]"))
            if not value_text:
                continue

            aid_text = clean_text(row.get("AID"))
            sid_text = clean_text(row.get("SID"))
            cid_text = clean_text(row.get("CID"))
            if not aid_text or not sid_text or not cid_text:
                continue

            try:
                aid = parse_positive_int(aid_text)
                sid = parse_positive_int(sid_text)
                cid = parse_positive_int(cid_text)
                ic50_value = parse_positive_float(value_text)
            except Exception:
                continue

            external_key = f"aid:{aid}|sid:{sid}|cid:{cid}"

            kept += 1
            if kept % 5000 == 0:
                _log(f"Parsed kept={kept} rows (scanned={scanned})...")

            yield {
                "aid": aid,
                "sid": sid,
                "cid": cid,
                "activity_name": activity_name or "IC50",
                "activity_outcome": clean_text(row.get("Activity Outcome")),
                "assay_name": clean_text(row.get("Assay Name")),
                "ic50_value": ic50_value,
                "ic50_unit": "uM",
                "raw_row": dict(row),
                "external_key": external_key,
            }

        _log(f"PubChem concise scan complete. scanned={scanned} kept={kept}")

    def enrich_batch(self, rows: list[dict]) -> list[dict]:
        cids = sorted({row.get("cid") for row in rows if row.get("cid")})
        if not cids:
            return rows

        properties_url_base = f"{self.base_url}/compound/cid"
        metadata: Dict[int, Dict] = {}

        for batch in _chunked(cids, self.cid_batch_size):
            cid_csv = ",".join(str(cid) for cid in batch)
            url = (
                f"{properties_url_base}/{cid_csv}/property/"
                "CanonicalSMILES,ConnectivitySMILES,Title,InChIKey/JSON"
            )
            payload = get_json(url, None, self.http_config, label="PubChem")
            for item in (payload.get("PropertyTable") or {}).get("Properties") or []:
                cid = item.get("CID")
                if cid is None:
                    continue
                title = clean_text(item.get("Title"))
                smiles = clean_text(item.get("CanonicalSMILES")) or clean_text(item.get("ConnectivitySMILES"))
                inchikey = clean_text(item.get("InChIKey"))
                metadata[int(cid)] = {
                    "title": title,
                    "smiles": smiles,
                    "inchikey": inchikey,
                }

        for row in rows:
            row["cid_metadata"] = metadata.get(row.get("cid"), {})

        return rows

    def map_row(self, row: dict) -> StagedRecord:
        cid = row.get("cid")
        if not cid:
            raise ValueError("Missing CID.")

        ic50_value = row.get("ic50_value")
        if ic50_value is None:
            raise ValueError("Missing IC50 value.")

        ic50_unit = normalize_ic50_unit(row.get("ic50_unit"))

        metadata = row.get("cid_metadata") or {}
        title = clean_text(metadata.get("title"))
        smiles = clean_text(metadata.get("smiles"))
        inchikey = clean_text(metadata.get("inchikey"))

        compound = CompoundInput(
            canonical_smiles=smiles,
            standard_inchikey=inchikey,
            identifiers=build_identifier_inputs({"pubchem_cid": str(cid)}, "pubchem_cid"),
            names=build_name_inputs(preferred_name=title),
        )

        source_record = SourceRecordInput(
            source_name=self.source_name,
            source_record_key=f"aid:{row['aid']}|sid:{row['sid']}|cid:{cid}",
            record_type="assay_row",
            raw_payload={
                "concise_row": row.get("raw_row"),
                "cid_metadata": metadata,
            },
        )

        measurement = Ic50Input(
            ic50_value=float(ic50_value),
            ic50_unit=ic50_unit,
            qualifier="=",
            endpoint="IC50",
        )

        external_key = clean_text(row.get("external_key")) or source_record.source_record_key
        return StagedRecord(
            external_key=external_key,
            compound=compound,
            source_record=source_record,
            measurement=measurement,
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
    parser = argparse.ArgumentParser(description="Ingest hERG IC50 data from PubChem.")
    parser.add_argument("--pubchem-base-url", default=PUBCHEM_BASE_URL)
    parser.add_argument("--target-gene-symbol", default="KCNH2")
    parser.add_argument("--target-gene-id", default="3757")
    parser.add_argument("--activity-name-regex", default=r"(?i)\bic50\b")
    parser.add_argument("--cid-batch-size", type=int, default=150)

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
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
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
    db_config = _build_db_config(args)

    adapter = PubChemAdapter(
        http_config=http_config,
        base_url=args.pubchem_base_url,
        target_gene_symbol=args.target_gene_symbol,
        target_gene_id=args.target_gene_id,
        activity_name_regex=args.activity_name_regex,
        cid_batch_size=args.cid_batch_size,
    )

    stats = run_pipeline(adapter, db_config, run_config)

    _log("")
    _log("Ingestion summary")
    _log("-----------------")
    _log(f"Source: {adapter.source_name}")
    _log(f"Processed rows: {stats.processed}")
    _log(f"Stored rows: {stats.stored}")
    _log(f"Skipped invalid: {stats.skipped_invalid}")
    _log(f"Failed inserts: {stats.failed}")

    return 1 if stats.failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
