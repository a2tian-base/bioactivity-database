#!/usr/bin/env python3
"""
Ingest hERG (KCNH2) IC50 data from PubChem into the local PostgreSQL schema.

Default data source:
https://pubchem.ncbi.nlm.nih.gov/rest/pug/assay/target/genesymbol/KCNH2/concise/CSV

This script:
- filters rows to GeneID=3757 (KCNH2) and Activity Name matching IC50
- interprets Activity Value [uM] and converts to nM
- registers compounds via register_compound(...) using pubchem_cid
- inserts rows into ic50_results with source_ref tagged by PubChem AID/SID/CID
- supports idempotent reruns by skipping existing PubChem source_ref keys
"""

import argparse
import csv
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


PUBCHEM_BASE_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
SOURCE_REF_PREFIX = "PubChem:"
DEFAULT_CONCISE_PATH = "/assay/target/genesymbol/{gene_symbol}/concise/CSV"


def env_first(*keys: str, default: Optional[str] = None) -> Optional[str]:
    for key in keys:
        value = os.getenv(key)
        if value is not None and value != "":
            return value
    return default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest PubChem hERG IC50 data into PostgreSQL.")
    parser.add_argument("--pubchem-base-url", default=PUBCHEM_BASE_URL)
    parser.add_argument("--target-gene-symbol", default="KCNH2")
    parser.add_argument("--target-gene-id", default="3757")
    parser.add_argument("--activity-name-regex", default=r"(?i)\bic50\b")
    parser.add_argument("--concise-timeout-seconds", type=int, default=180)
    parser.add_argument("--request-timeout-seconds", type=int, default=45)
    parser.add_argument("--http-retries", type=int, default=4)
    parser.add_argument("--cid-batch-size", type=int, default=150)
    parser.add_argument("--max-activities", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")

    parser.add_argument("--db-host", default=env_first("DB_HOST", default="localhost"))
    parser.add_argument("--db-port", type=int, default=int(env_first("DB_PORT", default="5432")))
    parser.add_argument("--db-name", default=env_first("DB_NAME", "POSTGRES_DB", default="herg"))
    parser.add_argument("--db-user", default=env_first("DB_USER", "POSTGRES_USER", default="herg_user"))
    parser.add_argument("--db-password", default=env_first("DB_PASSWORD", "POSTGRES_PASSWORD", default="change_me"))
    return parser.parse_args()


def log(message: str) -> None:
    print(message, flush=True)


def chunked(values: Sequence[int], size: int) -> Iterable[Sequence[int]]:
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


def http_get_json(
    url: str,
    params: Optional[Dict[str, object]],
    timeout_seconds: int,
    retries: int,
) -> Dict:
    if params:
        encoded = urllib.parse.urlencode(params, doseq=True)
        full_url = f"{url}?{encoded}" if encoded else url
    else:
        full_url = url

    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(full_url, timeout=timeout_seconds) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            retryable = exc.code in {429, 500, 502, 503, 504}
            if attempt < retries and retryable:
                sleep_seconds = 2**attempt
                log(f"HTTP {exc.code} from PubChem, retrying in {sleep_seconds}s: {full_url}")
                time.sleep(sleep_seconds)
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries:
                sleep_seconds = 2**attempt
                log(f"Network error from PubChem, retrying in {sleep_seconds}s: {full_url}")
                time.sleep(sleep_seconds)
                continue
            raise


def parse_positive_float(value: str) -> float:
    parsed = float(value.strip())
    if parsed <= 0:
        raise ValueError("value must be > 0")
    return parsed


def to_nm_from_um(value_um: float) -> float:
    return value_um * 1000.0


def clean_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def fetch_filtered_activities(args: argparse.Namespace) -> Tuple[List[Dict], Set[int]]:
    pattern = re.compile(args.activity_name_regex)
    concise_url = args.pubchem_base_url.rstrip("/") + DEFAULT_CONCISE_PATH.format(
        gene_symbol=args.target_gene_symbol
    )

    filtered_rows: List[Dict] = []
    cid_set: Set[int] = set()
    scanned = 0
    kept = 0

    with urllib.request.urlopen(concise_url, timeout=args.concise_timeout_seconds) as response:
        reader = csv.DictReader((line.decode("utf-8", "ignore") for line in response))
        for row in reader:
            scanned += 1

            gene_id = clean_text(row.get("Target GeneID"))
            if gene_id != args.target_gene_id:
                continue

            activity_name = clean_text(row.get("Activity Name"))
            if not pattern.search(activity_name):
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
                aid = int(aid_text)
                sid = int(sid_text)
                cid = int(cid_text)
                value_um = parse_positive_float(value_text)
            except Exception:
                continue

            source_ref = (
                f"{SOURCE_REF_PREFIX}AID={aid};SID={sid};CID={cid};"
                f"GeneID={gene_id};ActivityName={activity_name or 'IC50'}"
            )

            filtered_rows.append(
                {
                    "aid": aid,
                    "sid": sid,
                    "cid": cid,
                    "activity_name": activity_name or "IC50",
                    "activity_outcome": clean_text(row.get("Activity Outcome")),
                    "assay_name": clean_text(row.get("Assay Name")),
                    "source_ref": source_ref,
                    "ic50_nm": to_nm_from_um(value_um),
                }
            )
            cid_set.add(cid)
            kept += 1

            if args.max_activities is not None and kept >= args.max_activities:
                break

            if kept > 0 and kept % 5000 == 0:
                log(f"Parsed kept={kept} rows (scanned={scanned})...")

    log(f"PubChem concise scan complete. scanned={scanned} kept={kept} unique_cids={len(cid_set)}")
    return filtered_rows, cid_set


def fetch_cid_properties(args: argparse.Namespace, cids: Sequence[int]) -> Dict[int, Dict]:
    properties_url_base = args.pubchem_base_url.rstrip("/") + "/compound/cid"
    result: Dict[int, Dict] = {}
    sorted_cids = sorted(set(cids))

    for batch_index, batch in enumerate(chunked(sorted_cids, args.cid_batch_size), start=1):
        cid_csv = ",".join(str(cid) for cid in batch)
        url = (
            f"{properties_url_base}/{cid_csv}/property/"
            "CanonicalSMILES,ConnectivitySMILES,Title/JSON"
        )
        payload = http_get_json(
            url=url,
            params=None,
            timeout_seconds=args.request_timeout_seconds,
            retries=args.http_retries,
        )
        for item in (payload.get("PropertyTable") or {}).get("Properties") or []:
            cid = item.get("CID")
            if cid is None:
                continue
            title = clean_text(item.get("Title"))
            smiles = clean_text(item.get("CanonicalSMILES")) or clean_text(item.get("ConnectivitySMILES"))
            result[int(cid)] = {
                "title": title,
                "smiles": smiles,
            }

        log(
            f"Fetched CID metadata batch {batch_index} "
            f"({len(result)}/{len(sorted_cids)} resolved)."
        )

    return result


def get_existing_pubchem_source_refs(cur: psycopg.Cursor) -> Set[str]:
    cur.execute(
        """
        SELECT source_ref
        FROM ic50_results
        WHERE source_ref LIKE %s
        """,
        (f"{SOURCE_REF_PREFIX}%",),
    )
    return {str(row[0]) for row in cur.fetchall() if row and row[0]}


def register_compound(cur: psycopg.Cursor, cid: int, smiles: str, title: str) -> int:
    common_names = [title] if title else []
    cur.execute(
        """
        SELECT register_compound(%s, %s, %s, %s, %s, %s::text[])
        """,
        (
            None,
            None,
            cid,
            None,
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
        VALUES (%s, %s, 'nM', '=', %s)
        """,
        (compound_id, ic50_nm, source_ref),
    )


def run(args: argparse.Namespace) -> int:
    log("Starting PubChem hERG ingestion...")
    filtered_rows, cid_set = fetch_filtered_activities(args)
    if not filtered_rows:
        log("No rows matched the PubChem filters. Exiting.")
        return 0

    cid_metadata = fetch_cid_properties(args, list(cid_set))

    processed = 0
    inserted = 0
    skipped_existing = 0
    skipped_invalid = 0
    failed = 0
    failed_examples: List[str] = []

    compound_id_cache: Dict[int, int] = {}
    db_kwargs = {
        "host": args.db_host,
        "port": args.db_port,
        "dbname": args.db_name,
        "user": args.db_user,
        "password": args.db_password,
    }

    with psycopg.connect(**db_kwargs) as conn:
        with conn.cursor() as cur:
            existing_refs = get_existing_pubchem_source_refs(cur)
            log(f"Found {len(existing_refs)} existing PubChem source_ref rows.")

            for row in filtered_rows:
                processed += 1
                source_ref = row["source_ref"]

                if source_ref in existing_refs:
                    skipped_existing += 1
                    continue

                cid = row["cid"]
                metadata = cid_metadata.get(cid, {})
                smiles = clean_text(metadata.get("smiles"))
                title = clean_text(metadata.get("title"))
                ic50_nm = float(row["ic50_nm"])

                if ic50_nm <= 0:
                    skipped_invalid += 1
                    continue

                if args.dry_run:
                    inserted += 1
                    continue

                cur.execute("SAVEPOINT ingest_row")
                try:
                    if cid not in compound_id_cache:
                        compound_id_cache[cid] = register_compound(
                            cur=cur,
                            cid=cid,
                            smiles=smiles,
                            title=title,
                        )

                    insert_ic50_result(
                        cur=cur,
                        compound_id=compound_id_cache[cid],
                        ic50_nm=ic50_nm,
                        source_ref=source_ref,
                    )
                    cur.execute("RELEASE SAVEPOINT ingest_row")
                    inserted += 1
                    existing_refs.add(source_ref)
                except Exception as exc:
                    cur.execute("ROLLBACK TO SAVEPOINT ingest_row")
                    cur.execute("RELEASE SAVEPOINT ingest_row")
                    failed += 1
                    if len(failed_examples) < 20:
                        failed_examples.append(
                            f"AID={row['aid']} SID={row['sid']} CID={cid}: {exc}"
                        )

                if processed % 5000 == 0:
                    log(
                        f"Processed={processed} inserted={inserted} "
                        f"skipped_existing={skipped_existing} skipped_invalid={skipped_invalid} failed={failed}"
                    )

    mode_label = "DRY RUN (would insert)" if args.dry_run else "INSERTED"
    log("")
    log("Ingestion summary")
    log("-----------------")
    log(f"Target Gene Symbol: {args.target_gene_symbol}")
    log(f"Target Gene ID: {args.target_gene_id}")
    log(f"Activity regex: {args.activity_name_regex}")
    log(f"Processed rows: {processed}")
    log(f"{mode_label}: {inserted}")
    log(f"Skipped existing: {skipped_existing}")
    log(f"Skipped invalid: {skipped_invalid}")
    log(f"Failed inserts: {failed}")

    if failed_examples:
        log("")
        log("Example row issues (up to 20):")
        for message in failed_examples:
            log(f"- {message}")

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
