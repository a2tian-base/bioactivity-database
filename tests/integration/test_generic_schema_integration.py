import os

import psycopg
import pytest

from herg.config import DbConfig
from herg.db import get_conn, upsert_compound, upsert_source_record
from herg.models import CompoundInput, SourceRecordInput
from herg.normalize import build_identifier_inputs


pytestmark = pytest.mark.skipif(os.getenv("HERG_TEST_DB") != "1", reason="set HERG_TEST_DB=1 to run")


SCHEMA_FIXTURE_SOURCE = "generic_schema_fixture"
SCHEMA_FIXTURE_INCHIKEY = "GENERICTESTSCHEMA-UHFFFAOYSA-N"


def _cleanup(cur):
    cur.execute(
        """
        DELETE FROM bioactivity_results
        WHERE source_record_id IN (
            SELECT source_record_id
            FROM source_records
            WHERE source_name = %s
        )
        """,
        (SCHEMA_FIXTURE_SOURCE,),
    )
    cur.execute(
        """
        DELETE FROM ingestion_runs
        WHERE source_name = %s
        """,
        (SCHEMA_FIXTURE_SOURCE,),
    )
    cur.execute(
        """
        DELETE FROM source_records
        WHERE source_name = %s
        """,
        (SCHEMA_FIXTURE_SOURCE,),
    )
    cur.execute(
        """
        DELETE FROM compounds
        WHERE standard_inchikey = %s
           OR compound_id IN (
               SELECT compound_id
               FROM compound_identifiers
               WHERE namespace = 'generic_schema_fixture'
           )
        """,
        (SCHEMA_FIXTURE_INCHIKEY,),
    )


def _assert_rejected(cur, sql: str, params: tuple = ()) -> None:
    cur.execute("SAVEPOINT rejected_statement")
    with pytest.raises(psycopg.Error):
        cur.execute(sql, params)
    cur.execute("ROLLBACK TO SAVEPOINT rejected_statement")
    cur.execute("RELEASE SAVEPOINT rejected_statement")


def _fetch_herg_endpoint_id(cur) -> int:
    cur.execute("SELECT endpoint_id FROM endpoints WHERE endpoint_key = 'herg_ic50'")
    return int(cur.fetchone()[0])


def _insert_fixture_compound_and_source(cur) -> tuple[int, int]:
    compound_id = upsert_compound(
        cur,
        CompoundInput(
            standard_inchikey=SCHEMA_FIXTURE_INCHIKEY,
            identifiers=build_identifier_inputs({"generic_schema_fixture": "compound-1"}, "generic_schema_fixture"),
        ),
    )
    source_record_id = upsert_source_record(
        cur,
        SourceRecordInput(
            source_name=SCHEMA_FIXTURE_SOURCE,
            source_record_key="source:1",
            record_type="fixture",
            raw_payload={"fixture": True},
        ),
    )
    return compound_id, source_record_id


def test_generic_tables_exist_and_seeded_herg_endpoint_is_available():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT to_regclass('endpoints'), to_regclass('ingestion_runs'), to_regclass('bioactivity_results')
            """
        )
        assert cur.fetchone() == ("endpoints", "ingestion_runs", "bioactivity_results")

        cur.execute("SELECT to_regclass('ic50_results')")
        assert cur.fetchone()[0] == "ic50_results"

        cur.execute(
            """
            SELECT
                display_name,
                active,
                spec->'measurement'->>'type',
                spec->'measurement'->>'value_kind',
                source_configs ? 'chembl',
                source_configs ? 'pubchem',
                spec_hash
            FROM endpoints
            WHERE endpoint_key = 'herg_ic50'
            """
        )
        row = cur.fetchone()

        assert row is not None
        display_name, active, measurement_type, value_kind, has_chembl, has_pubchem, spec_hash = row
        assert display_name == "hERG IC50"
        assert active is True
        assert measurement_type == "IC50"
        assert value_kind == "concentration"
        assert has_chembl is True
        assert has_pubchem is True
        assert spec_hash


def test_endpoint_json_type_constraints_reject_non_objects():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        _assert_rejected(
            cur,
            """
            INSERT INTO endpoints (endpoint_key, display_name, spec, source_configs, spec_hash)
            VALUES ('invalid_spec_json', 'Invalid Spec JSON', '[]'::jsonb, '{}'::jsonb, 'invalid-spec-json')
            """,
        )
        _assert_rejected(
            cur,
            """
            INSERT INTO endpoints (endpoint_key, display_name, spec, source_configs, spec_hash)
            VALUES ('invalid_config_json', 'Invalid Config JSON', '{}'::jsonb, '[]'::jsonb, 'invalid-config-json')
            """,
        )
        conn.commit()


def test_ingestion_run_constraints_reject_invalid_status_and_negative_counts():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        endpoint_id = _fetch_herg_endpoint_id(cur)

        _assert_rejected(
            cur,
            """
            INSERT INTO ingestion_runs (endpoint_id, source_name, query_hash, status)
            VALUES (%s, %s, 'query-1', 'done')
            """,
            (endpoint_id, SCHEMA_FIXTURE_SOURCE),
        )
        _assert_rejected(
            cur,
            """
            INSERT INTO ingestion_runs (endpoint_id, source_name, query_hash, status, rows_seen)
            VALUES (%s, %s, 'query-2', 'running', -1)
            """,
            (endpoint_id, SCHEMA_FIXTURE_SOURCE),
        )
        conn.commit()


def test_bioactivity_result_constraints_reject_invalid_value_kind_and_duplicate_key():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        _cleanup(cur)
        endpoint_id = _fetch_herg_endpoint_id(cur)
        compound_id, source_record_id = _insert_fixture_compound_and_source(cur)

        _assert_rejected(
            cur,
            """
            INSERT INTO bioactivity_results (
                endpoint_id,
                compound_id,
                source_record_id,
                result_key,
                measurement_type,
                value_kind
            )
            VALUES (%s, %s, %s, 'invalid-kind', 'IC50', 'boolean')
            """,
            (endpoint_id, compound_id, source_record_id),
        )

        cur.execute(
            """
            INSERT INTO bioactivity_results (
                endpoint_id,
                compound_id,
                source_record_id,
                result_key,
                measurement_type,
                value_kind,
                original_value,
                original_unit,
                original_relation,
                standard_value,
                standard_unit,
                standard_relation,
                p_value,
                p_value_relation
            )
            VALUES (%s, %s, %s, 'fixture-result', 'IC50', 'concentration', 100, 'nM', '=', 0.1, 'uM', '=', 7, '=')
            """,
            (endpoint_id, compound_id, source_record_id),
        )
        _assert_rejected(
            cur,
            """
            INSERT INTO bioactivity_results (
                endpoint_id,
                compound_id,
                source_record_id,
                result_key,
                measurement_type,
                value_kind
            )
            VALUES (%s, %s, %s, 'fixture-result', 'IC50', 'concentration')
            """,
            (endpoint_id, compound_id, source_record_id),
        )

        _cleanup(cur)
        conn.commit()
