#!/usr/bin/env python3
"""
Ingest hERG (KCNH2) IC50 data from ChEMBL into the local PostgreSQL schema.
"""

from __future__ import annotations

import argparse
import sys
from typing import Dict, List, Optional, Sequence, Tuple

from .db import get_conn, upsert_compound, upsert_ic50_result, upsert_source_record
from .ingest_common import chunked, env_first, http_get_json, log
from .models import CompoundInput, Ic50Input, SourceRecordInput
from .normalization import (
    build_identifier_inputs,
    build_name_inputs,
    clean_text,
    dedupe_casefolded,
    normalize_ic50_unit,
    normalize_qualifier,
)


CHEMBL_BASE_URL = "https://www.ebi.ac.uk/chembl/api/data"
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest hERG IC50 data from ChEMBL into PostgreSQL.")
    parser.add_argument("--chembl-base-url", default=CHEMBL_BASE_URL)
    parser.add_argument("--target-chembl-id", default="CHEMBL240")
    parser.add_argument("--standard-type", default="IC50")
    parser.add_argument("--relations", default="=,<,>")
    parser.add_argument("--activity-page-size", type=int, default=1000)
    parser.add_argument("--molecule-batch-size", type=int, default=150)
    parser.add_argument("--max-activities", type=int, default=None)
    parser.add_argument("--request-timeout-seconds", type=int, default=45)
    parser.add_argument("--http-retries", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")

    parser.add_argument("--db-host", default=env_first("DB_HOST", default="localhost"))
    parser.add_argument("--db-port", type=int, default=int(env_first("DB_PORT", default="5432")))
    parser.add_argument("--db-name", default=env_first("DB_NAME", "POSTGRES_DB", default="herg"))
    parser.add_argument("--db-user", default=env_first("DB_USER", "POSTGRES_USER", default="herg_user"))
    parser.add_argument("--db-password", default=env_first("DB_PASSWORD", "POSTGRES_PASSWORD", default="change_me"))
    return parser.parse_args()


def fetch_chembl_release(args: argparse.Namespace) -> str:
    status_url = f"{args.chembl_base_url}/status.json"
    status = http_get_json(status_url, {}, args.request_timeout_seconds, args.http_retries, label="ChEMBL")
    return str(status.get("chembl_db_version") or "unknown")


def fetch_activities(args: argparse.Namespace) -> List[Dict]:
    activities_url = f"{args.chembl_base_url}/activity.json"
    offset = 0
    collected: List[Dict] = []
    total_count: Optional[int] = None

    while True:
        params = {
            "target_chembl_id": args.target_chembl_id,
            "standard_type": args.standard_type,
            "standard_relation__in": args.relations,
            "data_validity_comment__isnull": "true",
            "only": ACTIVITY_ONLY_FIELDS,
            "limit": args.activity_page_size,
            "offset": offset,
        }
        payload = http_get_json(activities_url, params, args.request_timeout_seconds, args.http_retries, label="ChEMBL")
        page = payload.get("activities") or []
        page_meta = payload.get("page_meta") or {}

        if total_count is None:
            total_count = int(page_meta.get("total_count") or 0)
            log(f"ChEMBL reported {total_count} candidate activities for {args.target_chembl_id}.")

        if not page:
            break

        remaining = None if args.max_activities is None else max(args.max_activities - len(collected), 0)
        if remaining is not None and remaining == 0:
            break

        if remaining is not None:
            page = page[:remaining]

        collected.extend(page)
        offset += len(page)
        log(f"Fetched {len(collected)} activities so far...")

        if len(page) < args.activity_page_size:
            break
        if args.max_activities is not None and len(collected) >= args.max_activities:
            break

    return collected


def fetch_molecule_metadata(args: argparse.Namespace, molecule_ids: Sequence[str]) -> Dict[str, Dict]:
    molecules_url = f"{args.chembl_base_url}/molecule.json"
    result: Dict[str, Dict] = {}
    ids_sorted = sorted(set(molecule_ids))

    for batch_idx, batch in enumerate(chunked(ids_sorted, args.molecule_batch_size), start=1):
        params = {
            "molecule_chembl_id__in": ",".join(batch),
            "only": MOLECULE_ONLY_FIELDS,
            "limit": len(batch),
        }
        payload = http_get_json(molecules_url, params, args.request_timeout_seconds, args.http_retries, label="ChEMBL")
        molecules = payload.get("molecules") or []
        for molecule in molecules:
            chembl_id = molecule.get("molecule_chembl_id")
            if chembl_id:
                result[str(chembl_id)] = molecule
        log(
            f"Fetched molecule metadata batch {batch_idx} "
            f"({len(result)}/{len(ids_sorted)} molecules resolved)."
        )

    return result


def parse_float(value: object) -> float:
    text = str(value).strip()
    return float(text)


def extract_synonyms(molecule_payload: Dict) -> List[str]:
    names: List[str] = []
    for synonym in molecule_payload.get("molecule_synonyms") or []:
        value = synonym.get("molecule_synonym") or synonym.get("synonyms")
        if value:
            names.append(str(value))
    return dedupe_casefolded(names)[:50]


def run(args: argparse.Namespace) -> int:
    release = fetch_chembl_release(args)
    log(f"Detected ChEMBL release: {release}")

    activities = fetch_activities(args)
    if not activities:
        log("No activities were returned from ChEMBL. Exiting.")
        return 0

    molecule_ids = sorted(
        {
            str(activity.get("molecule_chembl_id")).strip()
            for activity in activities
            if activity.get("molecule_chembl_id")
        }
    )
    log(f"Unique molecules referenced by activity rows: {len(molecule_ids)}")
    molecule_map = fetch_molecule_metadata(args, molecule_ids)

    processed = 0
    stored = 0
    skipped_invalid = 0
    failed = 0
    failed_examples: List[Tuple[int, str]] = []

    compound_id_cache: Dict[str, int] = {}

    with get_conn(
        host=args.db_host,
        port=args.db_port,
        dbname=args.db_name,
        user=args.db_user,
        password=args.db_password,
    ) as conn:
        with conn.cursor() as cur:
            for activity in activities:
                processed += 1
                activity_id_raw = activity.get("activity_id")
                molecule_chembl_id = str(activity.get("molecule_chembl_id") or "").strip()
                relation = str(activity.get("standard_relation") or "").strip()
                standard_value = activity.get("standard_value")
                standard_units = str(activity.get("standard_units") or "").strip()

                try:
                    activity_id = int(activity_id_raw)
                except (TypeError, ValueError):
                    skipped_invalid += 1
                    continue

                molecule_payload = molecule_map.get(molecule_chembl_id)
                if molecule_payload is None:
                    skipped_invalid += 1
                    continue

                try:
                    value = parse_float(standard_value)
                    if value <= 0:
                        raise ValueError("IC50 value must be > 0")
                    ic50_unit = normalize_ic50_unit(standard_units)
                    qualifier = normalize_qualifier(relation)
                except Exception as exc:
                    skipped_invalid += 1
                    if len(failed_examples) < 20:
                        failed_examples.append((activity_id, f"Normalization error: {exc}"))
                    continue

                molecule_structures = molecule_payload.get("molecule_structures") or {}
                canonical_smiles = clean_text(molecule_structures.get("canonical_smiles"))
                standard_inchi = clean_text(molecule_structures.get("standard_inchi"))
                standard_inchikey = clean_text(molecule_structures.get("standard_inchi_key"))
                pref_name = clean_text(molecule_payload.get("pref_name"))
                synonyms = extract_synonyms(molecule_payload)

                compound_input = CompoundInput(
                    canonical_smiles=canonical_smiles,
                    standard_inchi=standard_inchi,
                    standard_inchikey=standard_inchikey,
                    identifiers=build_identifier_inputs({"chembl_id": molecule_chembl_id}, primary_namespace="chembl_id"),
                    names=build_name_inputs(preferred_name=pref_name, aliases=synonyms),
                )

                source_input = SourceRecordInput(
                    source_name="chembl",
                    source_record_key=f"activity:{activity_id}",
                    record_type="activity",
                    source_release=release,
                    raw_payload={
                        "activity": activity,
                        "molecule": molecule_payload,
                    },
                )

                ic50_input = Ic50Input(
                    ic50_value=value,
                    ic50_unit=ic50_unit,
                    qualifier=qualifier,
                    endpoint="IC50",
                )

                if args.dry_run:
                    stored += 1
                    continue

                cur.execute("SAVEPOINT ingest_row")
                try:
                    if molecule_chembl_id not in compound_id_cache:
                        compound_id_cache[molecule_chembl_id] = upsert_compound(cur, compound_input)

                    source_record_id = upsert_source_record(cur, source_input)
                    upsert_ic50_result(
                        cur,
                        compound_id_cache[molecule_chembl_id],
                        source_record_id,
                        ic50_input,
                    )
                    cur.execute("RELEASE SAVEPOINT ingest_row")
                    stored += 1
                except Exception as exc:
                    cur.execute("ROLLBACK TO SAVEPOINT ingest_row")
                    cur.execute("RELEASE SAVEPOINT ingest_row")
                    failed += 1
                    if len(failed_examples) < 20:
                        failed_examples.append((activity_id, str(exc)))

                if processed % 1000 == 0:
                    log(
                        f"Processed={processed} stored={stored} "
                        f"skipped_invalid={skipped_invalid} failed={failed}"
                    )

    mode_label = "DRY RUN (would store)" if args.dry_run else "STORED"
    log("")
    log("Ingestion summary")
    log("-----------------")
    log(f"Target: {args.target_chembl_id}")
    log(f"ChEMBL release: {release}")
    log(f"Processed rows: {processed}")
    log(f"{mode_label}: {stored}")
    log(f"Skipped invalid: {skipped_invalid}")
    log(f"Failed inserts: {failed}")

    if failed_examples:
        log("")
        log("Example row issues (up to 20):")
        for activity_id, message in failed_examples:
            log(f"- activity_id={activity_id}: {message}")

    return 1 if failed > 0 else 0


def main() -> int:
    args = parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        log("Interrupted.")
        return 130
    except Exception as exc:
        log(f"Fatal error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
