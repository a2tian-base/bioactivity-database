from __future__ import annotations

import os
from typing import Any

import pandas as pd
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from .models import CompoundInput, Ic50Input, SourceRecordInput


def _env_value(key: str, default: str) -> str:
    value = os.getenv(key)
    return value if value else default


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def get_conn(
    host: str | None = None,
    port: int | None = None,
    dbname: str | None = None,
    user: str | None = None,
    password: str | None = None,
) -> psycopg.Connection:
    resolved_port = port
    if resolved_port is None:
        resolved_port = int(_env_value("DB_PORT", "5432"))

    return psycopg.connect(
        host=host or _env_value("DB_HOST", "localhost"),
        port=resolved_port,
        dbname=dbname or _env_value("DB_NAME", "herg"),
        user=user or _env_value("DB_USER", "herg_user"),
        password=password or _env_value("DB_PASSWORD", "change_me"),
    )


def upsert_compound(cur: psycopg.Cursor, compound: CompoundInput) -> int:
    identifiers_payload = [
        {
            "namespace": identifier.namespace,
            "value": identifier.value,
            "is_primary": identifier.is_primary,
        }
        for identifier in compound.identifiers
    ]
    names_payload = [
        {
            "name": name.name,
            "name_type": name.name_type,
            "is_preferred": name.is_preferred,
        }
        for name in compound.names
    ]

    cur.execute(
        """
        SELECT register_compound_v2(%s::jsonb, %s::jsonb, %s, %s, %s)
        """,
        (
            Json(identifiers_payload),
            Json(names_payload),
            _optional_text(compound.canonical_smiles),
            _optional_text(compound.standard_inchi),
            _optional_text(compound.standard_inchikey),
        ),
    )
    row = cur.fetchone()
    return int(row[0])


def upsert_source_record(cur: psycopg.Cursor, source: SourceRecordInput) -> int:
    cur.execute(
        """
        SELECT upsert_source_record(%s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            _optional_text(source.source_name),
            _optional_text(source.source_record_key),
            _optional_text(source.record_type),
            _optional_text(source.source_release),
            _optional_text(source.source_url),
            Json(source.raw_payload or {}),
        ),
    )
    row = cur.fetchone()
    return int(row[0])


def upsert_ic50_result(
    cur: psycopg.Cursor,
    compound_id: int,
    source_record_id: int,
    measurement: Ic50Input,
) -> dict[str, Any]:
    cur.execute(
        """
        SELECT *
        FROM upsert_ic50_result(%s, %s, %s, %s, %s, %s)
        """,
        (
            compound_id,
            source_record_id,
            _optional_text(measurement.endpoint),
            measurement.ic50_value,
            measurement.ic50_unit,
            measurement.qualifier,
        ),
    )
    row = cur.fetchone()
    return {
        "result_id": row[0],
        "ic50_nm": row[1],
        "pic50": row[2],
        "pic50_qualifier": row[3],
    }


def resolve_compound_id(cur: psycopg.Cursor, id_type: str, id_value: str) -> int | None:
    cur.execute(
        """
        SELECT resolve_compound_id(%s, %s)
        """,
        (id_type, id_value),
    )
    row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def fetch_compounds() -> list[dict[str, Any]]:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                compound_id,
                a_number,
                unii,
                pubchem_cid,
                chembl_id,
                preferred_name,
                common_names,
                canonical_smiles,
                standard_inchi,
                standard_inchikey,
                created_at,
                updated_at
            FROM compound_summary_v
            ORDER BY
                COALESCE(
                    NULLIF(BTRIM(preferred_name), ''),
                    NULLIF(BTRIM(chembl_id), ''),
                    NULLIF(BTRIM(a_number), ''),
                    NULLIF(BTRIM(unii), ''),
                    pubchem_cid::TEXT,
                    compound_id::TEXT
                ) ASC
            """
        )
        rows = cur.fetchall()

    compounds: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        if record.get("common_names") is None:
            record["common_names"] = []
        compounds.append(record)
    return compounds


def fetch_results(limit: int) -> pd.DataFrame:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                result_id,
                compound_id,
                source_record_id,
                endpoint,
                ic50_value,
                ic50_unit,
                qualifier,
                ic50_nm,
                pic50,
                pic50_qualifier,
                created_at,
                updated_at,
                source_name,
                source_record_key,
                source_release,
                source_url,
                preferred_name,
                a_number,
                unii,
                pubchem_cid,
                chembl_id,
                standard_inchikey,
                compound_label
            FROM ic50_result_summary_v
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        columns = [desc.name for desc in cur.description]
    return pd.DataFrame(rows, columns=columns)


def fetch_dashboard_metrics() -> dict[str, Any]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM compound_summary_v) AS compounds_n,
                (SELECT COUNT(*) FROM ic50_result_summary_v) AS results_n,
                (SELECT COUNT(DISTINCT compound_id) FROM ic50_result_summary_v) AS compounds_with_results_n,
                (SELECT MIN(created_at) FROM ic50_result_summary_v) AS first_result_at,
                (SELECT MAX(created_at) FROM ic50_result_summary_v) AS last_result_at
            """
        )
        row = cur.fetchone()
    return {
        "compounds_n": row[0] or 0,
        "results_n": row[1] or 0,
        "compounds_with_results_n": row[2] or 0,
        "first_result_at": row[3],
        "last_result_at": row[4],
    }


def fetch_dashboard_data(limit: int) -> pd.DataFrame:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                result_id,
                compound_id,
                ic50_value,
                ic50_unit,
                qualifier,
                ic50_nm,
                pic50,
                pic50_qualifier,
                created_at,
                compound_label
            FROM ic50_result_summary_v
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        columns = [desc.name for desc in cur.description]
    return pd.DataFrame(rows, columns=columns)
