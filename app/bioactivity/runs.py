from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

import psycopg
from psycopg.types.json import Json


RUN_STATUSES = frozenset({"running", "succeeded", "failed", "partial"})


def _json_object(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


def _query_hash(query_config: Mapping[str, Any] | None) -> str:
    payload = json.dumps(_json_object(query_config), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def start_ingestion_run(
    cur: psycopg.Cursor,
    *,
    endpoint_id: int,
    source_name: str,
    source_release: str | None = None,
    query_config: Mapping[str, Any] | None = None,
) -> int:
    clean_source_name = str(source_name).strip()
    if not clean_source_name:
        raise ValueError("source_name is required.")

    config = _json_object(query_config)
    clean_source_release = str(source_release).strip() if source_release is not None else None
    cur.execute(
        """
        INSERT INTO ingestion_runs (
            endpoint_id,
            source_name,
            source_release,
            query_config,
            query_hash,
            status
        )
        VALUES (%s, %s, %s, %s::jsonb, %s, 'running')
        RETURNING ingestion_run_id
        """,
        (
            endpoint_id,
            clean_source_name,
            clean_source_release or None,
            Json(config),
            _query_hash(config),
        ),
    )
    return int(cur.fetchone()[0])


def finish_ingestion_run(
    cur: psycopg.Cursor,
    *,
    ingestion_run_id: int,
    status: str,
    counters: Mapping[str, Any],
    qc_summary: Mapping[str, Any] | None = None,
    error_summary: Mapping[str, Any] | None = None,
) -> None:
    clean_status = str(status).strip()
    if clean_status not in RUN_STATUSES:
        allowed = ", ".join(sorted(RUN_STATUSES))
        raise ValueError(f"Invalid ingestion run status '{clean_status}'. Allowed: {allowed}.")

    cur.execute(
        """
        UPDATE ingestion_runs
        SET
            status = %s,
            finished_at = NOW(),
            rows_seen = %s,
            rows_inserted = %s,
            rows_updated = %s,
            rows_skipped = %s,
            rows_failed = %s,
            qc_summary = %s::jsonb,
            error_summary = %s::jsonb
        WHERE ingestion_run_id = %s
        """,
        (
            clean_status,
            int(counters.get("rows_seen", 0)),
            int(counters.get("rows_inserted", 0)),
            int(counters.get("rows_updated", 0)),
            int(counters.get("rows_skipped", 0)),
            int(counters.get("rows_failed", 0)),
            Json(_json_object(qc_summary)),
            Json(_json_object(error_summary)),
            ingestion_run_id,
        ),
    )
