#!/usr/bin/env python3
"""
Ingest hERG (KCNH2) IC50 data from ChEMBL into the local PostgreSQL schema.

This script:
- pulls activities for target CHEMBL240 by default
- registers compounds through register_compound(...)
- inserts IC50 values in nM into ic50_results
- supports idempotent reruns by skipping existing ChEMBL activity IDs found in source_ref
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import psycopg


CHEMBL_BASE_URL = "https://www.ebi.ac.uk/chembl/api/data"
SOURCE_REF_PREFIX = "ChEMBL:activity_id="
VALID_RELATIONS = {"=", "<", ">"}
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


def env_first(*keys: str, default: Optional[str] = None) -> Optional[str]:
    for key in keys:
        value = os.getenv(key)
        if value is not None and value != "":
            return value
    return default


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


def log(message: str) -> None:
    print(message, flush=True)


def chunked(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


def http_get_json(
    url: str,
    params: Dict[str, object],
    timeout_seconds: int,
    retries: int,
) -> Dict:
    encoded = urllib.parse.urlencode(params, doseq=True)
    full_url = f"{url}?{encoded}" if encoded else url

    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(full_url, timeout=timeout_seconds) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            retryable = exc.code in {429, 500, 502, 503, 504}
            if attempt < retries and retryable:
                sleep_seconds = 2**attempt
                log(f"HTTP {exc.code} from ChEMBL, retrying in {sleep_seconds}s: {full_url}")
                time.sleep(sleep_seconds)
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries:
                sleep_seconds = 2**attempt
                log(f"Network error from ChEMBL, retrying in {sleep_seconds}s: {full_url}")
                time.sleep(sleep_seconds)
                continue
            raise


def fetch_chembl_release(args: argparse.Namespace) -> str:
    status_url = f"{args.chembl_base_url}/status.json"
    status = http_get_json(status_url, {}, args.request_timeout_seconds, args.http_retries)
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
        payload = http_get_json(activities_url, params, args.request_timeout_seconds, args.http_retries)
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
        payload = http_get_json(molecules_url, params, args.request_timeout_seconds, args.http_retries)
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


def to_nm(value: float, unit: str) -> float:
    normalized = unit.strip().replace("µ", "u").replace("μ", "u").replace(" ", "").lower()
    factors = {
        "pm": 1e-3,
        "nm": 1.0,
        "um": 1e3,
        "mm": 1e6,
        "m": 1e9,
    }
    if normalized not in factors:
        raise ValueError(f"Unsupported unit '{unit}'")
    return value * factors[normalized]


def extract_common_names(molecule_payload: Dict) -> List[str]:
    names: List[str] = []
    seen: Set[str] = set()

    def add_name(value: Optional[str]) -> None:
        if value is None:
            return
        cleaned = str(value).strip()
        if not cleaned:
            return
        key = cleaned.casefold()
        if key in seen:
            return
        seen.add(key)
        names.append(cleaned)

    add_name(molecule_payload.get("pref_name"))
    for synonym in molecule_payload.get("molecule_synonyms") or []:
        add_name(synonym.get("molecule_synonym") or synonym.get("synonyms"))

    return names[:50]


def get_existing_chembl_activity_ids(cur: psycopg.Cursor) -> Set[int]:
    cur.execute(
        """
        SELECT source_ref
        FROM ic50_results
        WHERE source_ref LIKE %s
        """,
        (f"{SOURCE_REF_PREFIX}%",),
    )
    activity_ids: Set[int] = set()
    regex = re.compile(r"^ChEMBL:activity_id=(\d+)")
    for (source_ref,) in cur.fetchall():
        if not source_ref:
            continue
        match = regex.match(source_ref)
        if match:
            activity_ids.add(int(match.group(1)))
    return activity_ids


def register_compound(cur: psycopg.Cursor, chembl_id: str, molecule_payload: Dict) -> int:
    smiles = ((molecule_payload.get("molecule_structures") or {}).get("canonical_smiles")) or ""
    common_names = extract_common_names(molecule_payload)
    cur.execute(
        """
        SELECT register_compound(%s, %s, %s, %s, %s, %s::text[])
        """,
        (
            None,
            None,
            None,
            chembl_id,
            smiles,
            common_names,
        ),
    )
    row = cur.fetchone()
    return int(row[0])


def insert_ic50_result(
    cur: psycopg.Cursor,
    compound_id: int,
    ic50_nm: float,
    qualifier: str,
    source_ref: str,
) -> None:
    cur.execute(
        """
        INSERT INTO ic50_results (
            compound_id,
            ic50_value,
            ic50_unit,
            qualifier,
            source_ref
        )
        VALUES (%s, %s, 'nM', %s, %s)
        """,
        (compound_id, ic50_nm, qualifier, source_ref),
    )


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
    inserted = 0
    skipped_existing = 0
    skipped_invalid = 0
    failed = 0
    failed_examples: List[Tuple[int, str]] = []

    compound_id_cache: Dict[str, int] = {}
    db_kwargs = {
        "host": args.db_host,
        "port": args.db_port,
        "dbname": args.db_name,
        "user": args.db_user,
        "password": args.db_password,
    }

    with psycopg.connect(**db_kwargs) as conn:
        with conn.cursor() as cur:
            existing_activity_ids = get_existing_chembl_activity_ids(cur)
            log(f"Found {len(existing_activity_ids)} existing ChEMBL activity IDs in ic50_results.")

            for activity in activities:
                processed += 1
                activity_id_raw = activity.get("activity_id")
                molecule_chembl_id = str(activity.get("molecule_chembl_id") or "").strip()
                relation = str(activity.get("standard_relation") or "").strip()
                assay_chembl_id = str(activity.get("assay_chembl_id") or "").strip()
                standard_value = activity.get("standard_value")
                standard_units = str(activity.get("standard_units") or "").strip()

                try:
                    activity_id = int(activity_id_raw)
                except (TypeError, ValueError):
                    skipped_invalid += 1
                    continue

                if activity_id in existing_activity_ids:
                    skipped_existing += 1
                    continue

                if relation not in VALID_RELATIONS:
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
                    ic50_nm = to_nm(value, standard_units)
                    if ic50_nm <= 0:
                        raise ValueError("Converted nM value must be > 0")
                except Exception as exc:
                    skipped_invalid += 1
                    if len(failed_examples) < 20:
                        failed_examples.append((activity_id, f"Normalization error: {exc}"))
                    continue

                source_ref = (
                    f"{SOURCE_REF_PREFIX}{activity_id};"
                    f"assay={assay_chembl_id};"
                    f"target={args.target_chembl_id};"
                    f"release={release}"
                )

                if args.dry_run:
                    inserted += 1
                    continue

                cur.execute("SAVEPOINT ingest_row")
                try:
                    if molecule_chembl_id not in compound_id_cache:
                        compound_id_cache[molecule_chembl_id] = register_compound(
                            cur=cur,
                            chembl_id=molecule_chembl_id,
                            molecule_payload=molecule_payload,
                        )

                    insert_ic50_result(
                        cur=cur,
                        compound_id=compound_id_cache[molecule_chembl_id],
                        ic50_nm=ic50_nm,
                        qualifier=relation,
                        source_ref=source_ref,
                    )
                    cur.execute("RELEASE SAVEPOINT ingest_row")
                    inserted += 1
                    existing_activity_ids.add(activity_id)
                except Exception as exc:
                    cur.execute("ROLLBACK TO SAVEPOINT ingest_row")
                    cur.execute("RELEASE SAVEPOINT ingest_row")
                    failed += 1
                    if len(failed_examples) < 20:
                        failed_examples.append((activity_id, str(exc)))

                if processed % 1000 == 0:
                    log(
                        f"Processed={processed} inserted={inserted} "
                        f"skipped_existing={skipped_existing} skipped_invalid={skipped_invalid} failed={failed}"
                    )

    mode_label = "DRY RUN (would insert)" if args.dry_run else "INSERTED"
    log("")
    log("Ingestion summary")
    log("-----------------")
    log(f"Target: {args.target_chembl_id}")
    log(f"ChEMBL release: {release}")
    log(f"Processed rows: {processed}")
    log(f"{mode_label}: {inserted}")
    log(f"Skipped existing: {skipped_existing}")
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
