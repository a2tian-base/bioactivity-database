from __future__ import annotations

import psycopg
from psycopg.types.json import Json

from .models import MeasurementInput


def upsert_bioactivity_result(
    cur: psycopg.Cursor,
    *,
    endpoint_id: int,
    compound_id: int,
    source_record_id: int,
    ingestion_run_id: int | None = None,
    measurement: MeasurementInput,
) -> int:
    cur.execute(
        """
        SELECT upsert_bioactivity_result(
            %s::bigint,
            %s::bigint,
            %s::bigint,
            %s::bigint,
            %s::text,
            %s::text,
            %s::text,
            %s::numeric,
            %s::text,
            %s::text,
            %s::numeric,
            %s::text,
            %s::text,
            %s::numeric,
            %s::text,
            %s::text,
            %s::jsonb,
            %s::jsonb
        )
        """,
        (
            endpoint_id,
            compound_id,
            source_record_id,
            ingestion_run_id,
            measurement.result_key,
            measurement.measurement_type,
            measurement.value_kind,
            measurement.original_value,
            measurement.original_unit,
            measurement.original_relation,
            measurement.standard_value,
            measurement.standard_unit,
            measurement.standard_relation,
            measurement.p_value,
            measurement.p_value_relation,
            measurement.value_text,
            Json(measurement.assay_context),
            Json(measurement.quality_flags),
        ),
    )
    row = cur.fetchone()
    return int(row[0])
