import json
from decimal import Decimal
from types import SimpleNamespace

from herg.config import DbConfig, RunConfig
from herg.models import CompoundInput, Ic50Input, SourceRecordInput, StagedRecord
from herg.normalize import build_identifier_inputs
from herg.pipeline import run_pipeline


class _DummyCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, *_args, **_kwargs):
        return None


class _DummyConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _DummyCursor(self)

    def commit(self):
        return None


class _MixedValidityAdapter:
    source_name = "fixture_pipeline"
    enrich_batch_size = 10

    def iter_raw_rows(self):
        yield {"external_key": "row:valid"}
        yield {"external_key": "row:missing_identity"}

    def enrich_batch(self, rows):
        return rows

    def map_row(self, row):
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
        if row["external_key"] == "row:missing_identity":
            compound = CompoundInput()
        else:
            compound = CompoundInput(
                identifiers=build_identifier_inputs({"fixture_id": "valid-1"}, "fixture_id"),
            )
        return StagedRecord(
            external_key=row["external_key"],
            compound=compound,
            source_record=source_record,
            measurement=measurement,
        )


class _SingleValidAdapter:
    source_name = "fixture_pipeline_single"
    effective_config = {"fixture": "single"}

    def iter_raw_rows(self):
        yield {"external_key": "row:valid"}

    def enrich_batch(self, rows):
        return rows

    def map_row(self, row):
        return StagedRecord(
            external_key=row["external_key"],
            compound=CompoundInput(
                identifiers=build_identifier_inputs({"fixture_id": "valid-1"}, "fixture_id"),
            ),
            source_record=SourceRecordInput(
                source_name=self.source_name,
                source_record_key=row["external_key"],
                record_type="fixture",
            ),
            measurement=Ic50Input(
                ic50_value=100.0,
                ic50_unit="nM",
                qualifier="=",
                endpoint="IC50",
            ),
        )


def _db_config() -> DbConfig:
    return DbConfig(host="localhost", port=5432, dbname="herg", user="herg_user", password="change_me")


def test_pipeline_dry_run_records_skipped_invalid_row(monkeypatch, tmp_path):
    monkeypatch.setattr("herg.pipeline.get_conn", lambda **kwargs: _DummyConnection())
    monkeypatch.setattr("herg.pipeline.ensure_measurement_ingest_schema", lambda cur: None)

    errors_path = tmp_path / "errors.jsonl"
    stats = run_pipeline(
        _MixedValidityAdapter(),
        DbConfig(host="localhost", port=5432, dbname="herg", user="herg_user", password="change_me"),
        RunConfig(dry_run=True, errors_path=str(errors_path)),
    )

    assert stats.processed == 2
    assert stats.stored == 1
    assert stats.skipped_invalid == 1
    assert stats.failed == 0

    error_lines = errors_path.read_text(encoding="utf-8").splitlines()
    assert len(error_lines) == 1
    error_payload = json.loads(error_lines[0])
    assert error_payload["source_name"] == "fixture_pipeline"
    assert error_payload["external_key"] == "row:missing_identity"
    assert "Compound requires at least one identifier" in error_payload["reason"]


def test_pipeline_keeps_legacy_ic50_dual_write_for_herg(monkeypatch):
    captured = {}

    monkeypatch.setattr("herg.pipeline.get_conn", lambda **kwargs: _DummyConnection())
    monkeypatch.setattr("herg.pipeline.ensure_measurement_ingest_schema", lambda cur: None)
    monkeypatch.setattr(
        "herg.pipeline.load_endpoint",
        lambda cur, endpoint_key: SimpleNamespace(endpoint_id=1, endpoint_key=endpoint_key),
    )
    monkeypatch.setattr("herg.pipeline.start_ingestion_run", lambda *args, **kwargs: 99)
    monkeypatch.setattr("herg.pipeline.finish_ingestion_run", lambda *args, **kwargs: None)
    monkeypatch.setattr("herg.pipeline.upsert_compound", lambda cur, compound: 11)
    monkeypatch.setattr("herg.pipeline.upsert_source_record", lambda cur, source_record: 22)

    def fake_upsert_ic50_result(cur, compound_id, source_record_id, measurement):
        captured["legacy_measurement"] = measurement
        return {
            "result_id": 33,
            "ic50_um": Decimal("0.100000"),
            "pic50": Decimal("7.0000"),
            "pic50_qualifier": "=",
        }

    def fake_upsert_bioactivity_result(*args, **kwargs):
        captured["generic_measurement"] = kwargs["measurement"]

    monkeypatch.setattr("herg.pipeline.upsert_ic50_result", fake_upsert_ic50_result)
    monkeypatch.setattr(
        "herg.pipeline._derive_ic50_result_values",
        lambda cur, measurement: (_ for _ in ()).throw(AssertionError("derive should not be used for hERG")),
    )
    monkeypatch.setattr("herg.pipeline.upsert_bioactivity_result", fake_upsert_bioactivity_result)

    stats = run_pipeline(_SingleValidAdapter(), _db_config(), RunConfig(dry_run=False), endpoint_key="herg_ic50")

    assert stats.stored == 1
    assert captured["legacy_measurement"].ic50_value == 100.0
    assert captured["generic_measurement"].standard_value == Decimal("0.100000")


def test_pipeline_skips_legacy_ic50_write_for_non_herg_endpoint(monkeypatch):
    captured = {}

    monkeypatch.setattr("herg.pipeline.get_conn", lambda **kwargs: _DummyConnection())
    monkeypatch.setattr("herg.pipeline.ensure_measurement_ingest_schema", lambda cur: None)
    monkeypatch.setattr(
        "herg.pipeline.load_endpoint",
        lambda cur, endpoint_key: SimpleNamespace(endpoint_id=2, endpoint_key=endpoint_key),
    )
    monkeypatch.setattr("herg.pipeline.start_ingestion_run", lambda *args, **kwargs: 100)
    monkeypatch.setattr("herg.pipeline.finish_ingestion_run", lambda *args, **kwargs: None)
    monkeypatch.setattr("herg.pipeline.upsert_compound", lambda cur, compound: 11)
    monkeypatch.setattr("herg.pipeline.upsert_source_record", lambda cur, source_record: 22)
    monkeypatch.setattr(
        "herg.pipeline.upsert_ic50_result",
        lambda cur, compound_id, source_record_id, measurement: (_ for _ in ()).throw(
            AssertionError("non-hERG endpoints must not write ic50_results")
        ),
    )
    monkeypatch.setattr(
        "herg.pipeline._derive_ic50_result_values",
        lambda cur, measurement: {
            "result_id": None,
            "ic50_um": Decimal("0.100000"),
            "pic50": Decimal("7.0000"),
            "pic50_qualifier": "=",
        },
    )

    def fake_upsert_bioactivity_result(*args, **kwargs):
        captured["endpoint_id"] = kwargs["endpoint_id"]
        captured["measurement"] = kwargs["measurement"]

    monkeypatch.setattr("herg.pipeline.upsert_bioactivity_result", fake_upsert_bioactivity_result)

    stats = run_pipeline(_SingleValidAdapter(), _db_config(), RunConfig(dry_run=False), endpoint_key="cyp3a4_ic50")

    assert stats.stored == 1
    assert captured["endpoint_id"] == 2
    assert captured["measurement"].measurement_type == "IC50"
    assert captured["measurement"].standard_value == Decimal("0.100000")
