#!/usr/bin/env python3
"""
Ingest hERG (KCNH2) IC50 data from PubChem into the local PostgreSQL schema.
"""

from __future__ import annotations

import argparse
import csv
import sys
import urllib.request
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .db import get_conn, upsert_compound, upsert_ic50_result, upsert_source_record
from .ingest_common import chunked, env_first, http_get_json, log
from .models import CompoundInput, Ic50Input, SourceRecordInput
from .normalization import (
    build_identifier_inputs,
    build_name_inputs,
    clean_text,
    normalize_ic50_unit,
)


PUBCHEM_BASE_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
DEFAULT_CONCISE_PATH = "/assay/target/genesymbol/{gene_symbol}/concise/CSV"


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


def parse_positive_float(value: str) -> float:
    parsed = float(value.strip())
    if parsed <= 0:
        raise ValueError("value must be > 0")
    return parsed


def fetch_filtered_activities(args: argparse.Namespace) -> Tuple[List[Dict], Set[int]]:
    import re

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
            raw_row = dict(row)

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

            filtered_rows.append(
                {
                    "aid": aid,
                    "sid": sid,
                    "cid": cid,
                    "activity_name": activity_name or "IC50",
                    "activity_outcome": clean_text(row.get("Activity Outcome")),
                    "assay_name": clean_text(row.get("Assay Name")),
                    "ic50_value": value_um,
                    "raw_row": raw_row,
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
            "CanonicalSMILES,ConnectivitySMILES,Title,InChIKey/JSON"
        )
        payload = http_get_json(
            url=url,
            params=None,
            timeout_seconds=args.request_timeout_seconds,
            retries=args.http_retries,
            label="PubChem",
        )
        for item in (payload.get("PropertyTable") or {}).get("Properties") or []:
            cid = item.get("CID")
            if cid is None:
                continue
            title = clean_text(item.get("Title"))
            smiles = clean_text(item.get("CanonicalSMILES")) or clean_text(item.get("ConnectivitySMILES"))
            inchikey = clean_text(item.get("InChIKey"))
            result[int(cid)] = {
                "title": title,
                "smiles": smiles,
                "inchikey": inchikey,
            }

        log(
            f"Fetched CID metadata batch {batch_index} "
            f"({len(result)}/{len(sorted_cids)} resolved)."
        )

    return result


def run(args: argparse.Namespace) -> int:
    log("Starting PubChem hERG ingestion...")
    filtered_rows, cid_set = fetch_filtered_activities(args)
    if not filtered_rows:
        log("No rows matched the PubChem filters. Exiting.")
        return 0

    cid_metadata = fetch_cid_properties(args, list(cid_set))

    processed = 0
    stored = 0
    skipped_invalid = 0
    failed = 0
    failed_examples: List[str] = []

    compound_id_cache: Dict[int, int] = {}

    with get_conn(
        host=args.db_host,
        port=args.db_port,
        dbname=args.db_name,
        user=args.db_user,
        password=args.db_password,
    ) as conn:
        with conn.cursor() as cur:
            for row in filtered_rows:
                processed += 1
                cid = row["cid"]
                metadata = cid_metadata.get(cid, {})
                smiles = clean_text(metadata.get("smiles"))
                title = clean_text(metadata.get("title"))
                inchikey = clean_text(metadata.get("inchikey"))
                ic50_value = float(row["ic50_value"])

                if ic50_value <= 0:
                    skipped_invalid += 1
                    continue

                compound_input = CompoundInput(
                    canonical_smiles=smiles,
                    standard_inchikey=inchikey,
                    identifiers=build_identifier_inputs({"pubchem_cid": str(cid)}, primary_namespace="pubchem_cid"),
                    names=build_name_inputs(preferred_name=title),
                )

                source_input = SourceRecordInput(
                    source_name="pubchem",
                    source_record_key=f"aid:{row['aid']}|sid:{row['sid']}|cid:{cid}",
                    record_type="assay_concise_row",
                    raw_payload={
                        "concise_row": row["raw_row"],
                        "cid_metadata": metadata,
                    },
                )

                ic50_input = Ic50Input(
                    ic50_value=ic50_value,
                    ic50_unit=normalize_ic50_unit("uM"),
                    qualifier="=",
                    endpoint="IC50",
                )

                if args.dry_run:
                    stored += 1
                    continue

                cur.execute("SAVEPOINT ingest_row")
                try:
                    if cid not in compound_id_cache:
                        compound_id_cache[cid] = upsert_compound(cur, compound_input)

                    source_record_id = upsert_source_record(cur, source_input)
                    upsert_ic50_result(cur, compound_id_cache[cid], source_record_id, ic50_input)
                    cur.execute("RELEASE SAVEPOINT ingest_row")
                    stored += 1
                except Exception as exc:
                    cur.execute("ROLLBACK TO SAVEPOINT ingest_row")
                    cur.execute("RELEASE SAVEPOINT ingest_row")
                    failed += 1
                    if len(failed_examples) < 20:
                        failed_examples.append(f"AID={row['aid']} SID={row['sid']} CID={cid}: {exc}")

                if processed % 5000 == 0:
                    log(
                        f"Processed={processed} stored={stored} "
                        f"skipped_invalid={skipped_invalid} failed={failed}"
                    )

    mode_label = "DRY RUN (would store)" if args.dry_run else "STORED"
    log("")
    log("Ingestion summary")
    log("-----------------")
    log(f"Target Gene Symbol: {args.target_gene_symbol}")
    log(f"Target Gene ID: {args.target_gene_id}")
    log(f"Activity regex: {args.activity_name_regex}")
    log(f"Processed rows: {processed}")
    log(f"{mode_label}: {stored}")
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
