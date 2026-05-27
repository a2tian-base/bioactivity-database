from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import psycopg

from .endpoints import EndpointConfig


@dataclass(frozen=True)
class ManualEntrySchema:
    supported: bool
    value_kind: str
    measurement_type: str = ""
    canonical_unit: str | None = None
    allowed_units: list[str] = field(default_factory=list)
    allowed_relations: list[str] = field(default_factory=list)
    message: str = ""


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_text_list(value: object, fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return fallback
    cleaned = [_clean_text(item) for item in value]
    return [item for item in cleaned if item] or fallback


def _format_decimal(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return str(value.normalize())
    return _clean_text(value)


def _relation_prefix(relation: object) -> str:
    cleaned = _clean_text(relation)
    return f"{cleaned} " if cleaned else ""


def _json_summary(value: object) -> str:
    if not value:
        return ""
    if isinstance(value, Mapping):
        return json.dumps(dict(value), sort_keys=True, default=str)
    return _clean_text(value)


def manual_entry_schema(endpoint: EndpointConfig) -> ManualEntrySchema:
    measurement = _as_dict(endpoint.spec.get("measurement"))
    normalization = _as_dict(endpoint.spec.get("normalization"))
    value_kind = _clean_text(measurement.get("value_kind"))
    measurement_type = _clean_text(measurement.get("type"))
    if value_kind != "concentration":
        label = value_kind or "unknown"
        return ManualEntrySchema(
            supported=False,
            value_kind=label,
            measurement_type=measurement_type,
            message=f"Manual entry currently supports concentration endpoints only; '{label}' is not supported.",
        )

    canonical_unit = _clean_text(measurement.get("canonical_unit")) or None
    allowed_units = _as_text_list(normalization.get("allowed_units"), [canonical_unit or "uM"])
    allowed_relations = _as_text_list(normalization.get("allowed_relations"), ["=", "<", ">"])
    return ManualEntrySchema(
        supported=True,
        value_kind=value_kind,
        measurement_type=measurement_type,
        canonical_unit=canonical_unit,
        allowed_units=allowed_units,
        allowed_relations=allowed_relations,
    )


ManualEntryConfig = ManualEntrySchema
manual_entry_config = manual_entry_schema


def _rows_from_cursor(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    columns = [desc.name for desc in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def count_bioactivity_results(conn_or_cur: psycopg.Connection | psycopg.Cursor, endpoint_id: int) -> int:
    def _count(cur: psycopg.Cursor) -> int:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM bioactivity_results
            WHERE endpoint_id = %s
            """,
            (endpoint_id,),
        )
        return int(cur.fetchone()[0] or 0)

    if hasattr(conn_or_cur, "fetchone"):
        return _count(conn_or_cur)

    with conn_or_cur.cursor() as cur:
        return _count(cur)


def fetch_bioactivity_results(
    conn_or_cur: psycopg.Connection | psycopg.Cursor,
    *,
    endpoint_id: int,
    limit: int | None = 100,
) -> list[dict[str, Any]]:
    def _fetch(cur: psycopg.Cursor) -> list[dict[str, Any]]:
        query = """
            SELECT
                br.result_id,
                br.endpoint_id,
                e.endpoint_key,
                e.display_name AS endpoint_display_name,
                br.compound_id,
                COALESCE(
                    NULLIF(BTRIM(c.preferred_name), ''),
                    NULLIF(BTRIM(c.chembl_id), ''),
                    NULLIF(BTRIM(c.a_number), ''),
                    NULLIF(BTRIM(c.unii), ''),
                    c.pubchem_cid::TEXT,
                    br.compound_id::TEXT
                ) AS compound_label,
                s.source_name,
                s.source_record_key,
                s.source_release,
                s.source_url,
                br.source_record_id,
                br.ingestion_run_id,
                br.measurement_type,
                br.value_kind,
                br.original_value,
                br.original_unit,
                br.original_relation,
                br.standard_value,
                br.standard_unit,
                br.standard_relation,
                br.p_value,
                br.p_value_relation,
                br.value_text,
                br.assay_context,
                br.quality_flags,
                br.created_at,
                br.updated_at
            FROM bioactivity_results br
            JOIN endpoints e ON e.endpoint_id = br.endpoint_id
            JOIN source_records s ON s.source_record_id = br.source_record_id
            LEFT JOIN compound_summary_v c ON c.compound_id = br.compound_id
            WHERE br.endpoint_id = %s
            ORDER BY br.created_at DESC, br.result_id DESC
        """
        params: tuple[Any, ...] = (endpoint_id,)
        if limit is not None:
            query += "\nLIMIT %s"
            params = (endpoint_id, limit)
        cur.execute(query, params)
        return _rows_from_cursor(cur)

    if hasattr(conn_or_cur, "fetchall"):
        return _fetch(conn_or_cur)

    with conn_or_cur.cursor() as cur:
        return _fetch(cur)


def format_bioactivity_result_row(row: Mapping[str, Any]) -> dict[str, Any]:
    measurement_type = _clean_text(row.get("measurement_type"))
    value_kind = _clean_text(row.get("value_kind"))
    standard_value = _format_decimal(row.get("standard_value"))
    standard_unit = _clean_text(row.get("standard_unit"))
    standard_relation = _relation_prefix(row.get("standard_relation"))
    original_value = _format_decimal(row.get("original_value"))
    original_unit = _clean_text(row.get("original_unit"))
    original_relation = _relation_prefix(row.get("original_relation"))
    p_value = _format_decimal(row.get("p_value"))
    p_value_relation = _relation_prefix(row.get("p_value_relation"))
    value_text = _clean_text(row.get("value_text"))

    if value_kind == "concentration":
        display_value = standard_value or original_value
        display_unit = standard_unit or original_unit
        display_relation = standard_relation if standard_value else original_relation
        value_display = " ".join(
            part
            for part in [
                measurement_type,
                f"{display_relation}{display_value}".strip(),
                display_unit,
            ]
            if part
        )
    elif value_text:
        value_display = f"{measurement_type}: {value_text}" if measurement_type else value_text
    else:
        value_display = measurement_type or value_kind

    p_value_label = "pIC50" if measurement_type.upper() == "IC50" else "p-value"
    p_value_display = f"{p_value_label} {p_value_relation}{p_value}".strip() if p_value else ""

    return {
        "result_id": row.get("result_id"),
        "compound_id": row.get("compound_id"),
        "compound": row.get("compound_label") or row.get("compound_id"),
        "source": row.get("source_name"),
        "source_record_key": row.get("source_record_key"),
        "measurement_type": measurement_type,
        "value_kind": value_kind,
        "measurement": value_display,
        "original_value": row.get("original_value"),
        "original_unit": original_unit,
        "original_relation": _clean_text(row.get("original_relation")),
        "standard_value": row.get("standard_value"),
        "standard_unit": standard_unit,
        "standard_relation": _clean_text(row.get("standard_relation")),
        "p_value": row.get("p_value"),
        "p_value_relation": _clean_text(row.get("p_value_relation")),
        "p_value_display": p_value_display,
        "value_text": value_text,
        "assay_context": _json_summary(row.get("assay_context")),
        "quality_flags": _json_summary(row.get("quality_flags")),
        "endpoint_id": row.get("endpoint_id"),
        "endpoint_key": row.get("endpoint_key"),
        "endpoint": row.get("endpoint_display_name"),
        "source_record_id": row.get("source_record_id"),
        "ingestion_run_id": row.get("ingestion_run_id"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
