#!/usr/bin/env python3
"""
PubChem source adapter for hERG IC50 ingestion.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Pattern, Sequence

from bioactivity.endpoints import EndpointConfig, get_source_config, load_endpoint
from bioactivity.models import MeasurementInput, measurement_from_ic50
from ..config import DbConfig, HttpConfig, RunConfig
from ..db import get_conn
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
DEFAULT_CID_BATCH_SIZE = 150


def _chunked(values: Sequence[int], size: int) -> Iterable[Sequence[int]]:
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


def _log(message: str) -> None:
    print(message, flush=True)


def _required_config_text(source_config: Mapping[str, Any], key: str) -> str:
    value = clean_text(source_config.get(key))
    if not value:
        raise ValueError(f"PubChem source config requires '{key}'.")
    return value


def _compile_activity_name_regex(value: str) -> Pattern[str]:
    try:
        return re.compile(value)
    except re.error as exc:
        raise ValueError(f"Invalid PubChem activity_name_regex '{value}'.") from exc


def _optional_positive_int(value: object, default: int) -> int:
    if value is None:
        return default
    return parse_positive_int(value)


def _normalize_pubchem_source_config(
    source_config: Mapping[str, Any],
    *,
    cid_batch_size: int | None = None,
) -> dict[str, object]:
    if not isinstance(source_config, Mapping):
        raise ValueError("PubChem source config must be a mapping.")

    activity_name_regex = _required_config_text(source_config, "activity_name_regex")
    _compile_activity_name_regex(activity_name_regex)

    return {
        "target_gene_symbol": _required_config_text(source_config, "target_gene_symbol"),
        "target_gene_id": _required_config_text(source_config, "target_gene_id"),
        "activity_name_regex": activity_name_regex,
        "cid_batch_size": cid_batch_size
        if cid_batch_size is not None
        else _optional_positive_int(source_config.get("cid_batch_size"), DEFAULT_CID_BATCH_SIZE),
    }


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
        self.activity_name_regex = activity_name_regex
        self.activity_name_pattern = _compile_activity_name_regex(activity_name_regex)
        self.cid_batch_size = cid_batch_size
        self.enrich_batch_size = max(1, cid_batch_size)

    @classmethod
    def from_source_config(
        cls,
        endpoint: EndpointConfig,
        source_config: Mapping[str, Any],
        *,
        http_config: HttpConfig,
        base_url: str = PUBCHEM_BASE_URL,
        cid_batch_size: int | None = None,
    ) -> "PubChemAdapter":
        config = _normalize_pubchem_source_config(source_config, cid_batch_size=cid_batch_size)
        return cls(
            http_config=http_config,
            base_url=base_url,
            target_gene_symbol=str(config["target_gene_symbol"]),
            target_gene_id=str(config["target_gene_id"]),
            activity_name_regex=str(config["activity_name_regex"]),
            cid_batch_size=int(config["cid_batch_size"]),
        )

    @property
    def effective_config(self) -> dict[str, object]:
        return {
            "target_gene_symbol": self.target_gene_symbol,
            "target_gene_id": self.target_gene_id,
            "activity_name_regex": self.activity_name_regex,
            "cid_batch_size": self.cid_batch_size,
        }

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


def measurement_input_from_pubchem_record(record: StagedRecord) -> MeasurementInput:
    concise_row = record.source_record.raw_payload.get("concise_row") or {}
    assay_context = {
        "aid": clean_text(concise_row.get("AID")),
        "sid": clean_text(concise_row.get("SID")),
        "cid": clean_text(concise_row.get("CID")),
        "activity_name": clean_text(concise_row.get("Activity Name")),
        "activity_outcome": clean_text(concise_row.get("Activity Outcome")),
        "assay_name": clean_text(concise_row.get("Assay Name")),
    }
    assay_context = {key: value for key, value in assay_context.items() if value}
    return measurement_from_ic50(
        result_key=record.external_key,
        ic50_value=record.measurement.ic50_value,
        ic50_unit=record.measurement.ic50_unit,
        qualifier=record.measurement.qualifier,
        assay_context=assay_context,
        quality_flags={"source": PubChemAdapter.source_name},
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
    parser.add_argument("--endpoint-key", default="herg_ic50")
    parser.add_argument("--pubchem-base-url", default=PUBCHEM_BASE_URL)
    parser.add_argument("--target-gene-symbol", default=None)
    parser.add_argument("--target-gene-id", default=None)
    parser.add_argument("--activity-name-regex", default=None)
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


def _pubchem_source_config_from_args(endpoint: EndpointConfig, args: argparse.Namespace) -> dict[str, object]:
    source_config = get_source_config(endpoint, PubChemAdapter.source_name)
    if args.target_gene_symbol:
        source_config["target_gene_symbol"] = args.target_gene_symbol
    if args.target_gene_id:
        source_config["target_gene_id"] = args.target_gene_id
    if args.activity_name_regex:
        source_config["activity_name_regex"] = args.activity_name_regex
    return source_config


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

    with get_conn(db_config=db_config) as conn:
        endpoint = load_endpoint(conn, args.endpoint_key)

    adapter = PubChemAdapter.from_source_config(
        endpoint,
        _pubchem_source_config_from_args(endpoint, args),
        http_config=http_config,
        base_url=args.pubchem_base_url,
        cid_batch_size=args.cid_batch_size,
    )

    stats = run_pipeline(adapter, db_config, run_config, endpoint_key=endpoint.endpoint_key)

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
