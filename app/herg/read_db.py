from __future__ import annotations

from typing import Any

try:
    import pandas as pd
except ImportError:  # pragma: no cover - pandas is part of runtime deps
    pd = None
import psycopg
from psycopg.rows import dict_row

from .config import DbConfig
from .db import get_conn


_IDENTIFIER_ENRICHMENT_WHERE = {
    "pubchem_cid": """
        pubchem_cid IS NULL
          AND (
              NULLIF(BTRIM(standard_inchikey), '') IS NOT NULL
              OR NULLIF(BTRIM(chembl_id), '') IS NOT NULL
              OR NULLIF(BTRIM(unii), '') IS NOT NULL
          )
    """,
    "chembl_id": """
        chembl_id IS NULL
          AND (
              NULLIF(BTRIM(standard_inchikey), '') IS NOT NULL
              OR pubchem_cid IS NOT NULL
              OR NULLIF(BTRIM(unii), '') IS NOT NULL
          )
    """,
    "unii": """
        unii IS NULL
          AND NULLIF(BTRIM(standard_inchikey), '') IS NOT NULL
    """,
}

_STRUCTURE_ENRICHMENT_WHERE = {
    "chembl": """
        chembl_id IS NOT NULL
          AND (
              NULLIF(BTRIM(canonical_smiles), '') IS NULL
              OR NULLIF(BTRIM(standard_inchi), '') IS NULL
              OR NULLIF(BTRIM(standard_inchikey), '') IS NULL
          )
    """,
    "pubchem": """
        pubchem_cid IS NOT NULL
          AND (
              NULLIF(BTRIM(standard_inchi), '') IS NULL
              OR NULLIF(BTRIM(standard_inchikey), '') IS NULL
          )
    """,
}


def _require_pandas():
    if pd is None:
        raise RuntimeError("pandas is required for DataFrame read helpers.")
    return pd


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


def fetch_results_count() -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM ic50_result_summary_v")
        row = cur.fetchone()
    return int(row[0] or 0)


def fetch_results(limit: int | None = None) -> pd.DataFrame:
    pandas = _require_pandas()
    query = """
        SELECT
            r.result_id,
            r.compound_id,
            r.source_record_id,
            r.endpoint,
            r.ic50_value,
            r.ic50_unit,
            r.qualifier,
            r.ic50_um,
            r.pic50,
            r.pic50_qualifier,
            r.created_at,
            r.updated_at,
            r.source_name,
            r.source_record_key,
            r.source_release,
            r.source_url,
            r.preferred_name,
            r.a_number,
            r.unii,
            r.pubchem_cid,
            r.chembl_id,
            r.standard_inchikey,
            c.canonical_smiles AS compound_smiles,
            r.compound_label
        FROM ic50_result_summary_v r
        JOIN compound_summary_v c
          ON c.compound_id = r.compound_id
        ORDER BY r.created_at DESC
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        query += "\nLIMIT %s"
        params = (limit,)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
        columns = [desc.name for desc in cur.description]
    return pandas.DataFrame(rows, columns=columns)


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
    pandas = _require_pandas()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                result_id,
                compound_id,
                ic50_value,
                ic50_unit,
                qualifier,
                ic50_um,
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
    return pandas.DataFrame(rows, columns=columns)


def fetch_identifier_enrichment_candidates(
    target_namespace: str,
    limit: int | None = None,
    db_config: DbConfig | None = None,
) -> list[dict[str, Any]]:
    where_clause = _IDENTIFIER_ENRICHMENT_WHERE.get(target_namespace)
    if where_clause is None:
        raise ValueError(f"Unsupported target_namespace '{target_namespace}'.")

    query = f"""
        SELECT
            compound_id,
            standard_inchikey,
            chembl_id,
            pubchem_cid,
            unii,
            preferred_name
        FROM compound_summary_v
        WHERE {where_clause}
        ORDER BY compound_id
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        query += "\nLIMIT %s"
        params = (limit,)

    with get_conn(db_config=db_config) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [dict(row) for row in rows]


def fetch_structure_enrichment_candidates(
    provider: str,
    limit: int | None = None,
    db_config: DbConfig | None = None,
) -> list[dict[str, Any]]:
    where_clause = _STRUCTURE_ENRICHMENT_WHERE.get(provider)
    if where_clause is None:
        raise ValueError(f"Unsupported provider '{provider}'.")

    query = f"""
        SELECT
            compound_id,
            standard_inchikey,
            chembl_id,
            pubchem_cid,
            unii,
            preferred_name,
            canonical_smiles,
            standard_inchi
        FROM compound_summary_v
        WHERE {where_clause}
        ORDER BY compound_id
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        query += "\nLIMIT %s"
        params = (limit,)

    with get_conn(db_config=db_config) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [dict(row) for row in rows]
