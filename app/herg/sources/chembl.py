#!/usr/bin/env python3
"""
ChEMBL source adapter for hERG IC50 ingestion.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from typing import Any, Dict, Iterable, List, Optional

from bioactivity.endpoints import EndpointConfig, get_source_config, load_endpoint
from bioactivity.models import MeasurementInput, measurement_from_ic50

from ..config import DbConfig, HttpConfig, RunConfig
from ..db import get_conn
from ..http import get_json
from ..models import CompoundInput, Ic50Input, SourceRecordInput, StagedRecord
from ..normalize import (
    build_identifier_inputs,
    build_name_inputs,
    clean_text,
    dedupe_casefolded,
    normalize_ic50_unit,
    normalize_qualifier,
    parse_bool,
    parse_positive_int,
    parse_positive_float,
)
from ..pipeline import run_pipeline


CHEMBL_BASE_URL = "https://www.ebi.ac.uk/chembl/api/data"
DEFAULT_CHEMBL_RELATIONS = "=,<,>"
DEFAULT_ACTIVITY_PAGE_SIZE = 1000
DEFAULT_MOLECULE_BATCH_SIZE = 150
ACTIVITY_ONLY_FIELDS = (
    "activity_id,"
    "assay_chembl_id,"
    "molecule_chembl_id,"
    "standard_relation,"
    "standard_value,"
    "standard_units,"
    "data_validity_comment"
)
MOLECULE_ONLY_FIELDS = "molecule_chembl_id,pref_name,molecule_structures,molecule_synonyms"


def _chunked(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


def _log(message: str) -> None:
    print(message, flush=True)


def _required_config_text(source_config: Mapping[str, Any], key: str) -> str:
    value = clean_text(source_config.get(key))
    if not value:
        raise ValueError(f"ChEMBL source config requires '{key}'.")
    return value


def _normalize_relations(value: object) -> str:
    if value is None:
        return DEFAULT_CHEMBL_RELATIONS
    if isinstance(value, str):
        relations = [clean_text(part) for part in value.split(",")]
    elif isinstance(value, Sequence):
        relations = [clean_text(part) for part in value]
    else:
        raise ValueError("ChEMBL source config 'standard_relation__in' must be a string or sequence.")

    relations = [relation for relation in relations if relation]
    if not relations:
        raise ValueError("ChEMBL source config 'standard_relation__in' must not be empty.")
    return ",".join(relations)


def _optional_positive_int(value: object, default: int) -> int:
    if value is None:
        return default
    return parse_positive_int(value)


def _normalize_chembl_source_config(
    source_config: Mapping[str, Any],
    *,
    activity_page_size: int | None = None,
    molecule_batch_size: int | None = None,
) -> dict[str, object]:
    if not isinstance(source_config, Mapping):
        raise ValueError("ChEMBL source config must be a mapping.")

    data_validity_value = source_config.get("data_validity_comment__isnull", True)
    config_activity_page_size = source_config.get("page_size", source_config.get("activity_page_size"))

    return {
        "target_chembl_id": _required_config_text(source_config, "target_chembl_id"),
        "standard_type": _required_config_text(source_config, "standard_type"),
        "relations": _normalize_relations(source_config.get("standard_relation__in")),
        "data_validity_comment_isnull": parse_bool(data_validity_value),
        "activity_page_size": activity_page_size
        if activity_page_size is not None
        else _optional_positive_int(config_activity_page_size, DEFAULT_ACTIVITY_PAGE_SIZE),
        "molecule_batch_size": molecule_batch_size
        if molecule_batch_size is not None
        else _optional_positive_int(source_config.get("molecule_batch_size"), DEFAULT_MOLECULE_BATCH_SIZE),
    }


class ChemblAdapter:
    source_name = "chembl"

    def __init__(
        self,
        http_config: HttpConfig,
        base_url: str,
        target_chembl_id: str,
        standard_type: str,
        relations: str,
        activity_page_size: int,
        molecule_batch_size: int,
        data_validity_comment_isnull: bool = True,
    ) -> None:
        self.http_config = http_config
        self.base_url = base_url.rstrip("/")
        self.target_chembl_id = target_chembl_id
        self.standard_type = standard_type
        self.relations = relations
        self.activity_page_size = activity_page_size
        self.molecule_batch_size = molecule_batch_size
        self.data_validity_comment_isnull = data_validity_comment_isnull
        self.enrich_batch_size = max(1, activity_page_size)
        self.release = self._fetch_release()

    @classmethod
    def from_source_config(
        cls,
        endpoint: EndpointConfig,
        source_config: Mapping[str, Any],
        *,
        http_config: HttpConfig,
        base_url: str = CHEMBL_BASE_URL,
        activity_page_size: int | None = None,
        molecule_batch_size: int | None = None,
    ) -> "ChemblAdapter":
        config = _normalize_chembl_source_config(
            source_config,
            activity_page_size=activity_page_size,
            molecule_batch_size=molecule_batch_size,
        )
        return cls(
            http_config=http_config,
            base_url=base_url,
            target_chembl_id=str(config["target_chembl_id"]),
            standard_type=str(config["standard_type"]),
            relations=str(config["relations"]),
            data_validity_comment_isnull=bool(config["data_validity_comment_isnull"]),
            activity_page_size=int(config["activity_page_size"]),
            molecule_batch_size=int(config["molecule_batch_size"]),
        )

    @property
    def effective_config(self) -> dict[str, object]:
        return {
            "target_chembl_id": self.target_chembl_id,
            "standard_type": self.standard_type,
            "standard_relation__in": self.relations,
            "data_validity_comment__isnull": self.data_validity_comment_isnull,
            "activity_page_size": self.activity_page_size,
            "molecule_batch_size": self.molecule_batch_size,
        }

    def _fetch_release(self) -> str:
        status_url = f"{self.base_url}/status.json"
        status = get_json(status_url, {}, self.http_config, label="ChEMBL")
        return str(status.get("chembl_db_version") or "unknown")

    def iter_raw_rows(self) -> Iterable[dict]:
        activities_url = f"{self.base_url}/activity.json"
        offset = 0
        total_count: Optional[int] = None

        while True:
            params = {
                "target_chembl_id": self.target_chembl_id,
                "standard_type": self.standard_type,
                "standard_relation__in": self.relations,
                "data_validity_comment__isnull": "true" if self.data_validity_comment_isnull else "false",
                "only": ACTIVITY_ONLY_FIELDS,
                "limit": self.activity_page_size,
                "offset": offset,
            }
            payload = get_json(activities_url, params, self.http_config, label="ChEMBL")
            page = payload.get("activities") or []
            page_meta = payload.get("page_meta") or {}

            if total_count is None:
                total_count = int(page_meta.get("total_count") or 0)
                _log(f"ChEMBL reported {total_count} candidate activities for {self.target_chembl_id}.")

            if not page:
                break

            for activity in page:
                activity_id = clean_text(activity.get("activity_id"))
                external_key = f"activity:{activity_id}" if activity_id else ""
                yield {
                    "activity": activity,
                    "external_key": external_key,
                }

            offset += len(page)
            if len(page) < self.activity_page_size:
                break

    def enrich_batch(self, rows: list[dict]) -> list[dict]:
        molecule_ids = sorted(
            {
                clean_text(row.get("activity", {}).get("molecule_chembl_id"))
                for row in rows
                if clean_text(row.get("activity", {}).get("molecule_chembl_id"))
            }
        )
        molecule_map: Dict[str, Dict] = {}
        molecules_url = f"{self.base_url}/molecule.json"

        for batch in _chunked(molecule_ids, self.molecule_batch_size):
            params = {
                "molecule_chembl_id__in": ",".join(batch),
                "only": MOLECULE_ONLY_FIELDS,
                "limit": len(batch),
            }
            payload = get_json(molecules_url, params, self.http_config, label="ChEMBL")
            molecules = payload.get("molecules") or []
            for molecule in molecules:
                chembl_id = molecule.get("molecule_chembl_id")
                if chembl_id:
                    molecule_map[str(chembl_id)] = molecule

        for row in rows:
            chembl_id = clean_text(row.get("activity", {}).get("molecule_chembl_id"))
            row["molecule"] = molecule_map.get(chembl_id)
            row["source_release"] = self.release

        return rows

    def map_row(self, row: dict) -> StagedRecord:
        activity = row.get("activity") or {}
        molecule = row.get("molecule") or {}
        if not molecule:
            raise ValueError("Missing molecule metadata.")

        activity_id = clean_text(activity.get("activity_id"))
        if not activity_id:
            raise ValueError("Missing activity_id.")

        standard_value = parse_positive_float(activity.get("standard_value"))
        ic50_unit = normalize_ic50_unit(activity.get("standard_units"))
        qualifier = normalize_qualifier(activity.get("standard_relation"))

        molecule_structures = molecule.get("molecule_structures") or {}
        canonical_smiles = clean_text(molecule_structures.get("canonical_smiles"))
        standard_inchi = clean_text(molecule_structures.get("standard_inchi"))
        standard_inchikey = clean_text(molecule_structures.get("standard_inchi_key"))

        pref_name = clean_text(molecule.get("pref_name"))
        synonyms = self._extract_synonyms(molecule)

        compound = CompoundInput(
            canonical_smiles=canonical_smiles,
            standard_inchi=standard_inchi,
            standard_inchikey=standard_inchikey,
            identifiers=build_identifier_inputs({"chembl_id": clean_text(activity.get("molecule_chembl_id"))}, "chembl_id"),
            names=build_name_inputs(preferred_name=pref_name, aliases=synonyms),
        )

        source_record = SourceRecordInput(
            source_name=self.source_name,
            source_record_key=f"activity:{activity_id}",
            record_type="activity",
            source_release=clean_text(row.get("source_release")),
            raw_payload={
                "activity": activity,
                "molecule": molecule,
            },
        )

        measurement = Ic50Input(
            ic50_value=standard_value,
            ic50_unit=ic50_unit,
            qualifier=qualifier,
            endpoint="IC50",
        )

        return StagedRecord(
            external_key=f"activity:{activity_id}",
            compound=compound,
            source_record=source_record,
            measurement=measurement,
        )

    @staticmethod
    def _extract_synonyms(molecule_payload: Dict) -> List[str]:
        names: List[str] = []
        for synonym in molecule_payload.get("molecule_synonyms") or []:
            value = synonym.get("molecule_synonym") or synonym.get("synonyms")
            if value:
                names.append(str(value))
        return dedupe_casefolded(names)[:50]


def measurement_input_from_chembl_record(record: StagedRecord) -> MeasurementInput:
    activity = record.source_record.raw_payload.get("activity") or {}
    assay_chembl_id = clean_text(activity.get("assay_chembl_id"))
    assay_context = {"assay_chembl_id": assay_chembl_id} if assay_chembl_id else {}
    return measurement_from_ic50(
        result_key=record.external_key,
        ic50_value=record.measurement.ic50_value,
        ic50_unit=record.measurement.ic50_unit,
        qualifier=record.measurement.qualifier,
        assay_context=assay_context,
        quality_flags={"source": ChemblAdapter.source_name},
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
    parser = argparse.ArgumentParser(description="Ingest hERG IC50 data from ChEMBL.")
    parser.add_argument("--endpoint-key", default="herg_ic50")
    parser.add_argument("--chembl-base-url", default=CHEMBL_BASE_URL)
    parser.add_argument("--target-chembl-id", default=None)
    parser.add_argument("--standard-type", default=None)
    parser.add_argument("--relations", default=None)
    parser.add_argument("--activity-page-size", type=int, default=1000)
    parser.add_argument("--molecule-batch-size", type=int, default=150)

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


def _chembl_source_config_from_args(endpoint: EndpointConfig, args: argparse.Namespace) -> dict[str, object]:
    source_config = get_source_config(endpoint, ChemblAdapter.source_name)
    if args.target_chembl_id:
        source_config["target_chembl_id"] = args.target_chembl_id
    if args.standard_type:
        source_config["standard_type"] = args.standard_type
    if args.relations:
        source_config["standard_relation__in"] = args.relations
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

    adapter = ChemblAdapter.from_source_config(
        endpoint,
        _chembl_source_config_from_args(endpoint, args),
        http_config=http_config,
        base_url=args.chembl_base_url,
        activity_page_size=args.activity_page_size,
        molecule_batch_size=args.molecule_batch_size,
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
