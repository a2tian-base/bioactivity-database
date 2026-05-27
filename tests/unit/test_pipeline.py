import json
from decimal import Decimal
from types import SimpleNamespace

from bioactivity.models import measurement_from_ic50
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


def test_pipeline_non_dry_run_uses_adapter_measurement_mapper(monkeypatch):
    captured = {}

    monkeypatch.setattr("herg.pipeline.get_conn", lambda **kwargs: _DummyConnection())
    monkeypatch.setattr("herg.pipeline.ensure_measurement_ingest_schema", lambda cur: None)
    monkeypatch.setattr("herg.pipeline.load_endpoint", lambda cur, endpoint_key: SimpleNamespace(endpoint_id=99))
    monkeypatch.setattr("herg.pipeline.start_ingestion_run", lambda cur, **kwargs: 77)
    monkeypatch.setattr("herg.pipeline.finish_ingestion_run", lambda cur, **kwargs: None)
    monkeypatch.setattr("herg.pipeline.upsert_compound", lambda cur, compound: 10)
    monkeypatch.setattr("herg.pipeline.upsert_source_record", lambda cur, source_record: 20)
    monkeypatch.setattr(
        "herg.pipeline.upsert_ic50_result",
        lambda cur, compound_id, source_record_id, measurement: {
            "result_id": 30,
            "ic50_um": "0.100000",
            "pic50": "7.000000",
            "pic50_qualifier": "=",
        },
    )

    def fake_upsert_bioactivity_result(cur, **kwargs):
        captured.update(kwargs)
        return 40

    monkeypatch.setattr("herg.pipeline.upsert_bioactivity_result", fake_upsert_bioactivity_result)

    class Adapter:
        source_name = "fixture_context"
        effective_config = {"fixture": "context"}

        def iter_raw_rows(self):
            yield {"external_key": "row:context"}

        def enrich_batch(self, rows):
            return rows

        def map_row(self, row):
            return StagedRecord(
                external_key=row["external_key"],
                compound=CompoundInput(
                    identifiers=build_identifier_inputs({"fixture_id": "context-1"}, "fixture_id"),
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

        def measurement_input_from_record(self, record, ic50_result):
            return measurement_from_ic50(
                result_key=record.external_key,
                ic50_value=record.measurement.ic50_value,
                ic50_unit=record.measurement.ic50_unit,
                qualifier=record.measurement.qualifier,
                ic50_um=ic50_result.get("ic50_um"),
                pic50=ic50_result.get("pic50"),
                pic50_qualifier=ic50_result.get("pic50_qualifier"),
                assay_context={"assay_id": "A1"},
                quality_flags={"source": self.source_name},
            )

    stats = run_pipeline(
        Adapter(),
        DbConfig(host="localhost", port=5432, dbname="herg", user="herg_user", password="change_me"),
        RunConfig(dry_run=False),
    )

    measurement = captured["measurement"]
    assert stats.stored == 1
    assert captured["endpoint_id"] == 99
    assert captured["ingestion_run_id"] == 77
    assert measurement.assay_context == {"assay_id": "A1"}
    assert measurement.standard_value == Decimal("0.100000")
    assert measurement.standard_unit == "uM"
    assert measurement.p_value == Decimal("7.000000")
    assert measurement.p_value_relation == "="
