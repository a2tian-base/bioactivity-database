import os
from decimal import Decimal

import pytest

from herg.config import DbConfig, RunConfig
from herg.db import get_conn
from herg.models import CompoundInput, Ic50Input, SourceRecordInput, StagedRecord
from herg.normalize import build_identifier_inputs, build_name_inputs
from herg.pipeline import run_pipeline


PIPELINE_FIXTURE_SOURCE = "test_fixture"
PIPELINE_ERROR_SOURCE = "test_fixture_error"


def _cleanup(cur, source_name: str, namespace: str) -> None:
    cur.execute(
        """
        DELETE FROM bioactivity_results
        WHERE source_record_id IN (
            SELECT source_record_id
            FROM source_records
            WHERE source_name = %s
        )
        OR ingestion_run_id IN (
            SELECT ingestion_run_id
            FROM ingestion_runs
            WHERE source_name = %s
        )
        """,
        (source_name, source_name),
    )
    cur.execute(
        """
        DELETE FROM ic50_results
        WHERE source_record_id IN (
            SELECT source_record_id FROM source_records WHERE source_name = %s
        )
        """,
        (source_name,),
    )
    cur.execute(
        """
        DELETE FROM ingestion_runs
        WHERE source_name = %s
        """,
        (source_name,),
    )
    cur.execute(
        """
        DELETE FROM source_records
        WHERE source_name = %s
        """,
        (source_name,),
    )
    cur.execute(
        """
        DELETE FROM compounds
        WHERE compound_id IN (
            SELECT compound_id FROM compound_identifiers WHERE namespace = %s
        )
        """,
        (namespace,),
    )


@pytest.mark.skipif(os.getenv("HERG_TEST_DB") != "1", reason="set HERG_TEST_DB=1 to run")
def test_pipeline_dual_writes_generic_results_and_preserves_ic50_idempotence():
    db_config = DbConfig.from_env()

    class FixtureAdapter:
        source_name = PIPELINE_FIXTURE_SOURCE
        effective_config = {"fixture": True}

        def iter_raw_rows(self):
            yield {"external_key": "fixture:1"}

        def enrich_batch(self, rows):
            return rows

        def map_row(self, row):
            compound = CompoundInput(
                standard_inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
                identifiers=build_identifier_inputs({self.source_name: row["external_key"]}, self.source_name),
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

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        _cleanup(cur, adapter.source_name, adapter.source_name)
        conn.commit()

    first_stats = run_pipeline(adapter, db_config, run_config)
    second_stats = run_pipeline(adapter, db_config, run_config)

    assert first_stats.stored == 1
    assert first_stats.updated == 0
    assert second_stats.stored == 0
    assert second_stats.updated == 1

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

        cur.execute(
            """
            SELECT
                COUNT(*),
                ARRAY_AGG(status ORDER BY ingestion_run_id),
                ARRAY_AGG(rows_inserted ORDER BY ingestion_run_id),
                ARRAY_AGG(rows_updated ORDER BY ingestion_run_id)
            FROM ingestion_runs
            WHERE source_name = %s
            """,
            (adapter.source_name,),
        )
        run_count, run_statuses, run_inserted, run_updated = cur.fetchone()

        cur.execute(
            """
            SELECT
                COUNT(*),
                MIN(e.endpoint_key),
                MIN(br.compound_id),
                MIN(br.source_record_id),
                MIN(br.ingestion_run_id),
                MIN(br.measurement_type),
                MIN(br.value_kind),
                MIN(br.original_value),
                MIN(br.original_unit),
                MIN(br.original_relation),
                MIN(br.standard_value),
                MIN(br.standard_unit),
                MIN(br.p_value),
                MIN(br.p_value_relation)
            FROM bioactivity_results br
            JOIN endpoints e ON e.endpoint_id = br.endpoint_id
            WHERE br.source_record_id IN (
                SELECT source_record_id FROM source_records WHERE source_name = %s
            )
            """,
            (adapter.source_name,),
        )
        generic = cur.fetchone()

        assert source_count == 1
        assert results_count == 1
        assert ic50_um is not None
        assert pic50 is not None
        assert pic50_qualifier == "="
        assert run_count == 2
        assert run_statuses == ["succeeded", "succeeded"]
        assert run_inserted == [1, 0]
        assert run_updated == [0, 1]
        assert generic[0] == 1
        assert generic[1] == "herg_ic50"
        assert generic[2] is not None
        assert generic[3] is not None
        assert generic[4] is not None
        assert generic[5] == "IC50"
        assert generic[6] == "concentration"
        assert generic[7] == Decimal("100.0")
        assert generic[8] == "nM"
        assert generic[9] == "="
        assert generic[10] == Decimal("0.100000")
        assert generic[11] == "uM"
        assert generic[12] == pic50
        assert generic[13] == "="

        _cleanup(cur, adapter.source_name, adapter.source_name)
        conn.commit()


@pytest.mark.skipif(os.getenv("HERG_TEST_DB") != "1", reason="set HERG_TEST_DB=1 to run")
def test_pipeline_finalizes_partial_ingestion_run_when_adapter_raises_after_storing_row():
    db_config = DbConfig.from_env()

    class RaisingAdapter:
        source_name = PIPELINE_ERROR_SOURCE
        enrich_batch_size = 1
        effective_config = {"fixture": "raises"}

        def iter_raw_rows(self):
            yield {"external_key": "fixture:error:1"}
            raise RuntimeError("upstream fixture failure")

        def enrich_batch(self, rows):
            return rows

        def map_row(self, row):
            compound = CompoundInput(
                standard_inchikey="RAISINGPIPELINEFIX-UHFFFAOYSA-N",
                identifiers=build_identifier_inputs({self.source_name: row["external_key"]}, self.source_name),
            )
            source_record = SourceRecordInput(
                source_name=self.source_name,
                source_record_key=row["external_key"],
                record_type="fixture",
            )
            measurement = Ic50Input(
                ic50_value=250.0,
                ic50_unit="nM",
                qualifier="<",
                endpoint="IC50",
            )
            return StagedRecord(
                external_key=row["external_key"],
                compound=compound,
                source_record=source_record,
                measurement=measurement,
            )

    adapter = RaisingAdapter()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        _cleanup(cur, adapter.source_name, adapter.source_name)
        conn.commit()

    with pytest.raises(RuntimeError, match="upstream fixture failure"):
        run_pipeline(adapter, db_config, RunConfig(dry_run=False, commit_every=1))

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, rows_seen, rows_inserted, rows_skipped, rows_failed
            FROM ingestion_runs
            WHERE source_name = %s
            """,
            (adapter.source_name,),
        )
        assert cur.fetchone() == ("partial", 1, 1, 0, 1)

        cur.execute(
            """
            SELECT COUNT(*)
            FROM bioactivity_results
            WHERE source_record_id IN (
                SELECT source_record_id FROM source_records WHERE source_name = %s
            )
            """,
            (adapter.source_name,),
        )
        assert cur.fetchone()[0] == 1

        _cleanup(cur, adapter.source_name, adapter.source_name)
        conn.commit()
