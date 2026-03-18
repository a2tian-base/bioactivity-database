import os

import pytest

from herg.config import DbConfig, RunConfig
from herg.db import get_conn
from herg.models import CompoundInput, Ic50Input, SourceRecordInput, StagedRecord
from herg.normalize import build_identifier_inputs, build_name_inputs
from herg.pipeline import run_pipeline


@pytest.mark.skipif(os.getenv("HERG_TEST_DB") != "1", reason="set HERG_TEST_DB=1 to run")
def test_pipeline_idempotence_and_derivations():
    db_config = DbConfig.from_env()

    class FixtureAdapter:
        source_name = "test_fixture"

        def iter_raw_rows(self):
            yield {"external_key": "fixture:1"}

        def enrich_batch(self, rows):
            return rows

        def map_row(self, row):
            compound = CompoundInput(
                standard_inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
                identifiers=build_identifier_inputs({"test_fixture": row["external_key"]}, "test_fixture"),
                names=build_name_inputs(preferred_name="FixtureMol"),
            )
            source_record = SourceRecordInput(
                source_name=self.source_name,
                source_record_key=row["external_key"],
                record_type="fixture",
            )
            measurement = Ic50Input(
                ic50_value=100.0,
                ic50_unit="nM",
                qualifier="=",
                endpoint="IC50",
            )
            return StagedRecord(
                external_key=row["external_key"],
                compound=compound,
                source_record=source_record,
                measurement=measurement,
            )

    adapter = FixtureAdapter()
    run_config = RunConfig(dry_run=False, commit_every=1)

    run_pipeline(adapter, db_config, run_config)
    run_pipeline(adapter, db_config, run_config)

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM source_records
            WHERE source_name = %s
            """,
            (adapter.source_name,),
        )
        source_count = cur.fetchone()[0]

        cur.execute(
            """
            SELECT COUNT(*), MIN(ic50_um), MIN(pic50), MIN(pic50_qualifier)
            FROM ic50_results
            WHERE source_record_id IN (
                SELECT source_record_id FROM source_records WHERE source_name = %s
            )
            """,
            (adapter.source_name,),
        )
        results_count, ic50_um, pic50, pic50_qualifier = cur.fetchone()

        assert source_count == 1
        assert results_count == 1
        assert ic50_um is not None
        assert pic50 is not None
        assert pic50_qualifier == "="

        cur.execute(
            """
            DELETE FROM ic50_results
            WHERE source_record_id IN (
                SELECT source_record_id FROM source_records WHERE source_name = %s
            )
            """,
            (adapter.source_name,),
        )
        cur.execute(
            """
            DELETE FROM source_records
            WHERE source_name = %s
            """,
            (adapter.source_name,),
        )
        cur.execute(
            """
            DELETE FROM compounds
            WHERE compound_id IN (
                SELECT compound_id FROM compound_identifiers WHERE namespace = %s
            )
            """,
            ("test_fixture",),
        )
        conn.commit()
