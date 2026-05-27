import os
from decimal import Decimal

import pytest

from bioactivity.db import upsert_bioactivity_result
from bioactivity.models import MeasurementInput
from herg.config import DbConfig
from herg.db import get_conn, upsert_compound, upsert_source_record
from herg.models import CompoundInput, SourceRecordInput
from herg.normalize import build_identifier_inputs


pytestmark = pytest.mark.skipif(os.getenv("HERG_TEST_DB") != "1", reason="set HERG_TEST_DB=1 to run")


UPSERT_FIXTURE_SOURCE = "bioactivity_result_upsert_fixture"
UPSERT_FIXTURE_INCHIKEY = "BIOACTIVITYRESULTUP-UHFFFAOYSA-N"
ALT_ENDPOINT_KEY = "bioactivity_result_upsert_alt"


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
        (UPSERT_FIXTURE_SOURCE, ALT_ENDPOINT_KEY),
    )
    cur.execute(
        """
        DELETE FROM ingestion_runs
        WHERE source_name = %s
        """,
        (UPSERT_FIXTURE_SOURCE,),
    )
    cur.execute(
        """
        DELETE FROM source_records
        WHERE source_name = %s
        """,
        (UPSERT_FIXTURE_SOURCE,),
    )
    cur.execute(
        """
        DELETE FROM compounds
        WHERE standard_inchikey = %s
           OR compound_id IN (
               SELECT compound_id
               FROM compound_identifiers
               WHERE namespace = 'bioactivity_result_upsert_fixture'
           )
        """,
        (UPSERT_FIXTURE_INCHIKEY,),
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
            'Bioactivity Result Upsert Alt',
            '{"measurement": {"type": "inhibition", "value_kind": "percent"}}'::jsonb,
            '{}'::jsonb,
            'bioactivity-result-upsert-alt',
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
        (ALT_ENDPOINT_KEY,),
    )
    return int(cur.fetchone()[0])


def _insert_fixture_compound_and_source(cur, source_key: str = "source:1") -> tuple[int, int]:
    compound_id = upsert_compound(
        cur,
        CompoundInput(
            standard_inchikey=UPSERT_FIXTURE_INCHIKEY,
            identifiers=build_identifier_inputs(
                {"bioactivity_result_upsert_fixture": "compound-1"},
                "bioactivity_result_upsert_fixture",
            ),
        ),
    )
    source_record_id = upsert_source_record(
        cur,
        SourceRecordInput(
            source_name=UPSERT_FIXTURE_SOURCE,
            source_record_key=source_key,
            record_type="fixture",
            raw_payload={"source_record_key": source_key},
        ),
    )
    return compound_id, source_record_id


def _insert_ingestion_run(cur, endpoint_id: int) -> int:
    cur.execute(
        """
        INSERT INTO ingestion_runs (
            endpoint_id,
            source_name,
            query_config,
            query_hash,
            status,
            rows_seen
        )
        VALUES (%s, %s, '{"fixture": true}'::jsonb, 'fixture-query', 'running', 1)
        RETURNING ingestion_run_id
        """,
        (endpoint_id, UPSERT_FIXTURE_SOURCE),
    )
    return int(cur.fetchone()[0])


def test_upsert_inserts_concentration_result_with_run_and_json_context():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        _cleanup(cur)
        endpoint_id = _fetch_herg_endpoint_id(cur)
        compound_id, source_record_id = _insert_fixture_compound_and_source(cur)
        ingestion_run_id = _insert_ingestion_run(cur, endpoint_id)
        ic50_count_before = _count_ic50_results(cur)

        result_id = upsert_bioactivity_result(
            cur,
            endpoint_id=endpoint_id,
            compound_id=compound_id,
            source_record_id=source_record_id,
            ingestion_run_id=ingestion_run_id,
            measurement=MeasurementInput(
                result_key="activity:123",
                measurement_type="IC50",
                value_kind="concentration",
                original_value=Decimal("50"),
                original_unit="nM",
                original_relation="<",
                standard_value=Decimal("0.05"),
                standard_unit="uM",
                standard_relation="<",
                p_value=Decimal("7.3010"),
                p_value_relation=">",
                assay_context={"assay_chembl_id": "CHEMBL123"},
                quality_flags={"source": "fixture"},
            ),
        )

        cur.execute(
            """
            SELECT
                result_id,
                ingestion_run_id,
                measurement_type,
                value_kind,
                original_value,
                original_unit,
                original_relation,
                standard_value,
                standard_unit,
                standard_relation,
                p_value,
                p_value_relation,
                assay_context,
                quality_flags
            FROM bioactivity_results
            WHERE result_id = %s
            """,
            (result_id,),
        )
        row = cur.fetchone()

        assert row[0] == result_id
        assert row[1] == ingestion_run_id
        assert row[2] == "IC50"
        assert row[3] == "concentration"
        assert row[4] == Decimal("50")
        assert row[5] == "nM"
        assert row[6] == "<"
        assert row[7] == Decimal("0.05")
        assert row[8] == "uM"
        assert row[9] == "<"
        assert row[10] == Decimal("7.3010")
        assert row[11] == ">"
        assert row[12] == {"assay_chembl_id": "CHEMBL123"}
        assert row[13] == {"source": "fixture"}
        assert _count_ic50_results(cur) == ic50_count_before

        _cleanup(cur)
        conn.commit()


@pytest.mark.parametrize(
    "measurement",
    [
        MeasurementInput(
            result_key="percent:1",
            measurement_type="inhibition",
            value_kind="percent",
            standard_value=Decimal("72"),
            standard_unit="%",
        ),
        MeasurementInput(
            result_key="numeric:1",
            measurement_type="solubility",
            value_kind="numeric",
            standard_value=Decimal("12.5"),
            standard_unit="ug/mL",
        ),
        MeasurementInput(
            result_key="categorical:1",
            measurement_type="activity_outcome",
            value_kind="categorical",
            value_text="active",
        ),
        MeasurementInput(
            result_key="text:1",
            measurement_type="assay_note",
            value_kind="text",
            value_text="observed precipitate",
        ),
    ],
)
def test_upsert_supports_non_concentration_value_kinds(measurement):
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        _cleanup(cur)
        endpoint_id = _fetch_herg_endpoint_id(cur)
        compound_id, source_record_id = _insert_fixture_compound_and_source(cur, source_key=measurement.result_key)

        result_id = upsert_bioactivity_result(
            cur,
            endpoint_id=endpoint_id,
            compound_id=compound_id,
            source_record_id=source_record_id,
            measurement=measurement,
        )

        cur.execute(
            """
            SELECT value_kind, standard_value, standard_unit, value_text
            FROM bioactivity_results
            WHERE result_id = %s
            """,
            (result_id,),
        )
        assert cur.fetchone() == (
            measurement.value_kind,
            measurement.standard_value,
            measurement.standard_unit,
            measurement.value_text,
        )

        _cleanup(cur)
        conn.commit()


def test_reupsert_updates_existing_result_and_returns_same_id():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        _cleanup(cur)
        endpoint_id = _fetch_herg_endpoint_id(cur)
        compound_id, source_record_id = _insert_fixture_compound_and_source(cur)

        initial_id = upsert_bioactivity_result(
            cur,
            endpoint_id=endpoint_id,
            compound_id=compound_id,
            source_record_id=source_record_id,
            measurement=MeasurementInput(
                result_key="same-result",
                measurement_type="IC50",
                value_kind="concentration",
                standard_value=Decimal("1.0"),
                standard_unit="uM",
            ),
        )
        updated_id = upsert_bioactivity_result(
            cur,
            endpoint_id=endpoint_id,
            compound_id=compound_id,
            source_record_id=source_record_id,
            measurement=MeasurementInput(
                result_key="same-result",
                measurement_type="IC50",
                value_kind="concentration",
                standard_value=Decimal("2.0"),
                standard_unit="uM",
                quality_flags={"updated": True},
            ),
        )

        cur.execute(
            """
            SELECT COUNT(*), MIN(result_id), MIN(standard_value), MIN(quality_flags::text)
            FROM bioactivity_results
            WHERE endpoint_id = %s
              AND source_record_id = %s
              AND result_key = 'same-result'
            """,
            (endpoint_id, source_record_id),
        )
        count, stored_id, standard_value, quality_flags = cur.fetchone()

        assert updated_id == initial_id
        assert count == 1
        assert stored_id == initial_id
        assert standard_value == Decimal("2.0")
        assert quality_flags == '{"updated": true}'

        _cleanup(cur)
        conn.commit()


def test_same_result_key_is_allowed_for_different_endpoint():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        _cleanup(cur)
        endpoint_id = _fetch_herg_endpoint_id(cur)
        alt_endpoint_id = _insert_alt_endpoint(cur)
        compound_id, source_record_id = _insert_fixture_compound_and_source(cur)
        measurement = MeasurementInput(
            result_key="shared-result",
            measurement_type="inhibition",
            value_kind="percent",
            standard_value=Decimal("72"),
            standard_unit="%",
        )

        first_id = upsert_bioactivity_result(
            cur,
            endpoint_id=endpoint_id,
            compound_id=compound_id,
            source_record_id=source_record_id,
            measurement=measurement,
        )
        second_id = upsert_bioactivity_result(
            cur,
            endpoint_id=alt_endpoint_id,
            compound_id=compound_id,
            source_record_id=source_record_id,
            measurement=measurement,
        )

        cur.execute(
            """
            SELECT COUNT(*)
            FROM bioactivity_results
            WHERE source_record_id = %s
              AND result_key = 'shared-result'
            """,
            (source_record_id,),
        )

        assert second_id != first_id
        assert cur.fetchone()[0] == 2

        _cleanup(cur)
        conn.commit()


def test_invalid_value_kind_is_rejected_before_write():
    with pytest.raises(ValueError, match="Invalid value_kind"):
        MeasurementInput(
            result_key="invalid-kind",
            measurement_type="invalid",
            value_kind="boolean",
        )


def _count_ic50_results(cur) -> int:
    cur.execute("SELECT COUNT(*) FROM ic50_results")
    return int(cur.fetchone()[0])
