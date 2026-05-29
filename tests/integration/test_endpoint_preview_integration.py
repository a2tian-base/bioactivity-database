import os

import pytest
from psycopg.types.json import Json

from bioactivity.endpoints import MissingSourceConfigError
from bioactivity.preview import (
    PreviewExample,
    PreviewResult,
    UnsupportedSourceError,
    format_preview_result,
    preview_endpoint_source,
)
from herg.config import DbConfig
from herg.db import get_conn
from herg.models import CompoundInput, Ic50Input, SourceRecordInput, StagedRecord
from herg.normalize import build_identifier_inputs


requires_db = pytest.mark.skipif(os.getenv("HERG_TEST_DB") != "1", reason="set HERG_TEST_DB=1 to run")


MISSING_CONFIG_ENDPOINT_KEY = "endpoint_preview_missing_config_fixture"


class FakeChemblPreviewAdapter:
    source_name = "chembl"
    effective_config = {
        "target_chembl_id": "CHEMBL240",
        "standard_type": "IC50",
    }

    def __init__(self, source_config):
        self.source_config = source_config

    def iter_raw_rows(self):
        yield {"external_key": "activity:1", "kind": "accepted", "activity_id": "1"}
        yield {"external_key": "activity:bad", "kind": "skipped"}
        yield {"external_key": "activity:2", "kind": "accepted", "activity_id": "2"}

    def enrich_batch(self, rows):
        return rows

    def map_row(self, row):
        if row["kind"] == "skipped":
            raise ValueError("Missing molecule metadata.")

        compound = CompoundInput(
            standard_inchikey=f"PREVIEW{row['activity_id']}-UHFFFAOYSA-N",
            identifiers=build_identifier_inputs({"chembl_id": f"CHEMBL{row['activity_id']}"}),
        )
        source_record = SourceRecordInput(
            source_name=self.source_name,
            source_record_key=row["external_key"],
            record_type="activity",
            raw_payload={"activity": {"assay_chembl_id": f"CHEMBLASSAY{row['activity_id']}"}},
        )
        measurement = Ic50Input(
            ic50_value=50.0,
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


def _fake_chembl_factory(endpoint, source_config, http_config):
    assert endpoint.endpoint_key == "herg_ic50"
    assert source_config["target_chembl_id"] == "CHEMBL240"
    return FakeChemblPreviewAdapter(source_config)


def _table_counts(cur):
    counts = {}
    for table_name in ("source_records", "bioactivity_results", "ic50_results", "ingestion_runs"):
        cur.execute(f"SELECT COUNT(*) FROM {table_name}")
        counts[table_name] = int(cur.fetchone()[0])
    return counts


def _cleanup_endpoint(cur):
    cur.execute("DELETE FROM endpoints WHERE endpoint_key = %s", (MISSING_CONFIG_ENDPOINT_KEY,))


@requires_db
def test_preview_loads_herg_chembl_config_honors_limit_and_does_not_write():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        before_counts = _table_counts(cur)

        result = preview_endpoint_source(
            cur,
            endpoint_key="herg_ic50",
            source_name="chembl",
            limit=2,
            adapter_factories={"chembl": _fake_chembl_factory},
        )

        after_counts = _table_counts(cur)

    assert result.endpoint_key == "herg_ic50"
    assert result.source_name == "chembl"
    assert result.raw_rows_examined == 2
    assert result.accepted_count == 1
    assert result.skipped_count == 1
    assert result.error_count == 0
    assert result.accepted_examples[0].measurement["measurement_type"] == "IC50"
    assert result.accepted_examples[0].measurement["value_kind"] == "concentration"
    assert result.skipped_examples[0].reason == "Missing molecule metadata."
    assert after_counts == before_counts


@requires_db
def test_preview_unsupported_source_produces_clear_error():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        with pytest.raises(UnsupportedSourceError, match="Unsupported source 'unichem'"):
            preview_endpoint_source(
                cur,
                endpoint_key="herg_ic50",
                source_name="unichem",
                adapter_factories={"chembl": _fake_chembl_factory},
            )


@requires_db
def test_preview_missing_source_config_produces_clear_error():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        _cleanup_endpoint(cur)
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
            VALUES (%s, 'Endpoint Preview Missing Config', %s::jsonb, %s::jsonb, %s, TRUE)
            """,
            (
                MISSING_CONFIG_ENDPOINT_KEY,
                Json({"measurement": {"type": "IC50", "value_kind": "concentration"}}),
                Json({"chembl": {"target_chembl_id": "CHEMBL240", "standard_type": "IC50"}}),
                "endpoint-preview-missing-config-fixture",
            ),
        )

        with pytest.raises(MissingSourceConfigError, match="no source config for 'pubchem'"):
            preview_endpoint_source(
                cur,
                endpoint_key=MISSING_CONFIG_ENDPOINT_KEY,
                source_name="pubchem",
                adapter_factories={
                    "chembl": _fake_chembl_factory,
                    "pubchem": _fake_chembl_factory,
                },
            )

        _cleanup_endpoint(cur)
        conn.commit()


def test_preview_formatter_includes_summary_and_examples():
    result = PreviewResult(
        endpoint_key="herg_ic50",
        source_name="chembl",
        query_config={"target_chembl_id": "CHEMBL240"},
        raw_rows_examined=2,
        accepted_count=1,
        skipped_count=1,
        error_count=0,
        accepted_examples=[
            PreviewExample(
                external_key="activity:1",
                source_record_key="activity:1",
                measurement={
                    "measurement_type": "IC50",
                    "value_kind": "concentration",
                },
            )
        ],
        skipped_examples=[
            PreviewExample(
                external_key="activity:bad",
                reason="Missing molecule metadata.",
            )
        ],
        warnings=["fixture warning"],
    )

    text = format_preview_result(result)

    assert "Endpoint" in text
    assert "herg_ic50" in text
    assert "Source" in text
    assert "chembl" in text
    assert "Summary" in text
    assert "accepted: 1" in text
    assert "activity:1" in text
    assert "activity:bad" in text
    assert "fixture warning" in text
