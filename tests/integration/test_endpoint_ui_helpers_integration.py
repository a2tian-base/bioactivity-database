import os
from decimal import Decimal

import pytest
from psycopg.types.json import Json

from bioactivity.db import upsert_bioactivity_result
from bioactivity.endpoints import list_active_endpoints
from bioactivity.models import MeasurementInput
from bioactivity.results import fetch_bioactivity_results
from herg.config import DbConfig
from herg.db import get_conn, upsert_compound, upsert_source_record
from herg.models import CompoundInput, SourceRecordInput
from herg.normalize import build_identifier_inputs


pytestmark = pytest.mark.skipif(os.getenv("HERG_TEST_DB") != "1", reason="set HERG_TEST_DB=1 to run")


UI_FIXTURE_SOURCE = "endpoint_ui_helpers_fixture"
UI_FIXTURE_INCHIKEY = "ENDPOINTUIHELPERS-UHFFFAOYSA-N"
ALT_ENDPOINT_KEY = "endpoint_ui_helpers_alt"


def _cleanup(cur):
    cur.execute(
        """
        DELETE FROM bioactivity_results
        WHERE source_record_id IN (
            SELECT source_record_id
            FROM source_records
            WHERE source_name = %s
        )
        OR endpoint_id IN (
            SELECT endpoint_id
            FROM endpoints
            WHERE endpoint_key = %s
        )
        """,
        (UI_FIXTURE_SOURCE, ALT_ENDPOINT_KEY),
    )
    cur.execute(
        """
        DELETE FROM source_records
        WHERE source_name = %s
        """,
        (UI_FIXTURE_SOURCE,),
    )
    cur.execute(
        """
        DELETE FROM compounds
        WHERE standard_inchikey = %s
           OR compound_id IN (
               SELECT compound_id
               FROM compound_identifiers
               WHERE namespace = 'endpoint_ui_helpers_fixture'
           )
        """,
        (UI_FIXTURE_INCHIKEY,),
    )
    cur.execute(
        """
        DELETE FROM endpoints
        WHERE endpoint_key = %s
        """,
        (ALT_ENDPOINT_KEY,),
    )


def _fetch_herg_endpoint_id(cur) -> int:
    cur.execute("SELECT endpoint_id FROM endpoints WHERE endpoint_key = 'herg_ic50'")
    return int(cur.fetchone()[0])


def _insert_alt_endpoint(cur) -> int:
    cur.execute(
        """
        INSERT INTO endpoints (
            endpoint_key,
            display_name,
            spec,
            source_configs,
            spec_hash,
            active
        )
        VALUES (
            %s,
            'Endpoint UI Helpers Alt',
            %s::jsonb,
            '{}'::jsonb,
            'endpoint-ui-helpers-alt',
            TRUE
        )
        ON CONFLICT (endpoint_key)
        DO UPDATE SET
            display_name = EXCLUDED.display_name,
            spec = EXCLUDED.spec,
            source_configs = EXCLUDED.source_configs,
            spec_hash = EXCLUDED.spec_hash,
            active = EXCLUDED.active
        RETURNING endpoint_id
        """,
        (
            ALT_ENDPOINT_KEY,
            Json({"measurement": {"type": "Ames", "value_kind": "categorical"}}),
        ),
    )
    return int(cur.fetchone()[0])


def _insert_fixture_compound(cur) -> int:
    return upsert_compound(
        cur,
        CompoundInput(
            standard_inchikey=UI_FIXTURE_INCHIKEY,
            identifiers=build_identifier_inputs(
                {"endpoint_ui_helpers_fixture": "compound-1"},
                "endpoint_ui_helpers_fixture",
            ),
        ),
    )


def _insert_source(cur, source_key: str) -> int:
    return upsert_source_record(
        cur,
        SourceRecordInput(
            source_name=UI_FIXTURE_SOURCE,
            source_record_key=source_key,
            record_type="fixture",
            raw_payload={"source_record_key": source_key},
        ),
    )


def test_list_active_endpoints_returns_seeded_herg_ic50():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        endpoints = list_active_endpoints(cur)

    assert "herg_ic50" in {endpoint.endpoint_key for endpoint in endpoints}


def test_fetch_bioactivity_results_filters_by_endpoint_id():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        _cleanup(cur)
        herg_endpoint_id = _fetch_herg_endpoint_id(cur)
        alt_endpoint_id = _insert_alt_endpoint(cur)
        compound_id = _insert_fixture_compound(cur)
        herg_source_record_id = _insert_source(cur, "herg:1")
        alt_source_record_id = _insert_source(cur, "alt:1")

        upsert_bioactivity_result(
            cur,
            endpoint_id=herg_endpoint_id,
            compound_id=compound_id,
            source_record_id=herg_source_record_id,
            measurement=MeasurementInput(
                result_key="herg-result",
                measurement_type="IC50",
                value_kind="concentration",
                original_value=Decimal("100"),
                original_unit="nM",
                original_relation="=",
                standard_value=Decimal("0.1"),
                standard_unit="uM",
                standard_relation="=",
                p_value=Decimal("7"),
                p_value_relation="=",
            ),
        )
        upsert_bioactivity_result(
            cur,
            endpoint_id=alt_endpoint_id,
            compound_id=compound_id,
            source_record_id=alt_source_record_id,
            measurement=MeasurementInput(
                result_key="alt-result",
                measurement_type="Ames",
                value_kind="categorical",
                value_text="positive",
            ),
        )

        herg_rows = fetch_bioactivity_results(cur, endpoint_id=herg_endpoint_id, limit=None)
        alt_rows = fetch_bioactivity_results(cur, endpoint_id=alt_endpoint_id, limit=None)

        herg_source_ids = {row["source_record_id"] for row in herg_rows}
        alt_source_ids = {row["source_record_id"] for row in alt_rows}
        assert herg_source_record_id in herg_source_ids
        assert alt_source_record_id not in herg_source_ids
        assert alt_source_record_id in alt_source_ids
        assert herg_source_record_id not in alt_source_ids

        _cleanup(cur)
        conn.commit()
