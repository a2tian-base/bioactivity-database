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


def test_pipeline_skips_legacy_ic50_write_for_non_herg_endpoint(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setattr("herg.pipeline.get_conn", lambda **kwargs: _DummyConnection())
    monkeypatch.setattr("herg.pipeline.ensure_measurement_ingest_schema", lambda cur: None)
    monkeypatch.setattr("herg.pipeline.load_endpoint", lambda cur, endpoint_key: SimpleNamespace(endpoint_id=123))
    monkeypatch.setattr("herg.pipeline.start_ingestion_run", lambda *args, **kwargs: 456)
    monkeypatch.setattr("herg.pipeline.finish_ingestion_run", lambda *args, **kwargs: None)
    monkeypatch.setattr("herg.pipeline.upsert_compound", lambda cur, compound: 1)
    monkeypatch.setattr("herg.pipeline.upsert_source_record", lambda cur, source_record: 2)

    def fail_legacy_write(*_args, **_kwargs):
        raise AssertionError("non-hERG endpoints must not write ic50_results")

    def fake_derive_ic50_result_values(cur, measurement):
        captured["derived_measurement"] = measurement
        return {
            "result_id": None,
            "ic50_um": Decimal("0.1"),
            "pic50": Decimal("7"),
            "pic50_qualifier": "=",
        }

    def fake_upsert_bioactivity_result(cur, **kwargs):
        captured["bioactivity_kwargs"] = kwargs
        return 789

    monkeypatch.setattr("herg.pipeline.upsert_ic50_result", fail_legacy_write)
    monkeypatch.setattr("herg.pipeline._derive_ic50_result_values", fake_derive_ic50_result_values)
    monkeypatch.setattr("herg.pipeline.upsert_bioactivity_result", fake_upsert_bioactivity_result)

    stats = run_pipeline(
        _MixedValidityAdapter(),
        DbConfig(host="localhost", port=5432, dbname="herg", user="herg_user", password="change_me"),
        RunConfig(max_records=1, commit_every=1),
        endpoint_key="cyp3a4_ic50",
    )

    assert stats.processed == 1
    assert stats.stored == 1
    assert stats.failed == 0
    assert captured["derived_measurement"].ic50_unit == "nM"
    bioactivity_kwargs = captured["bioactivity_kwargs"]
    assert bioactivity_kwargs["endpoint_id"] == 123
    assert bioactivity_kwargs["ingestion_run_id"] == 456
    assert bioactivity_kwargs["measurement"].standard_value == Decimal("0.1")
