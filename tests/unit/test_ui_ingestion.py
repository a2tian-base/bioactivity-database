from types import SimpleNamespace

from bioactivity.endpoints import EndpointConfig
from bioactivity.ui_ingestion import UiIngestionRequest, run_ui_ingestion
from herg.config import DbConfig


class _DummyConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _endpoint() -> EndpointConfig:
    return EndpointConfig(
        endpoint_id=1,
        endpoint_key="herg_ic50",
        display_name="hERG IC50",
        spec={"measurement": {"type": "IC50", "value_kind": "concentration"}},
        source_configs={
            "chembl": {
                "target_chembl_id": "CHEMBL240",
                "standard_type": "IC50",
            }
        },
        spec_hash="fixture",
        active=True,
    )


def test_run_ui_ingestion_loads_endpoint_builds_adapter_and_calls_pipeline(monkeypatch):
    captured = {}

    def fake_get_conn(*, db_config):
        captured["db_config_for_conn"] = db_config
        return _DummyConnection()

    def fake_load_endpoint(conn, endpoint_key):
        captured["loaded_endpoint_key"] = endpoint_key
        return _endpoint()

    def fake_adapter_builder(**kwargs):
        captured["adapter_builder"] = kwargs
        return SimpleNamespace(source_name=kwargs["source_name"])

    def fake_pipeline(adapter, db_config, run_config, *, endpoint_key):
        captured["pipeline"] = {
            "adapter": adapter,
            "db_config": db_config,
            "run_config": run_config,
            "endpoint_key": endpoint_key,
        }
        return SimpleNamespace(
            processed=4,
            stored=3,
            updated=0,
            skipped_invalid=1,
            failed=0,
            warnings=2,
            duration_seconds=1.25,
            ingestion_run_id=123,
        )

    monkeypatch.setattr("bioactivity.ui_ingestion.get_conn", fake_get_conn)
    monkeypatch.setattr("bioactivity.ui_ingestion.load_endpoint", fake_load_endpoint)
    db_config = DbConfig(host="localhost", port=5432, dbname="herg", user="herg_user", password="change_me")

    result = run_ui_ingestion(
        UiIngestionRequest(
            endpoint_key="herg_ic50",
            source_name="ChEMBL",
            dry_run=True,
            max_records=25,
            commit_every=10,
            fail_fast=True,
            request_timeout_seconds=9,
            http_retries=1,
        ),
        db_config=db_config,
        adapter_builder=fake_adapter_builder,
        pipeline_runner=fake_pipeline,
    )

    assert captured["db_config_for_conn"] == db_config
    assert captured["loaded_endpoint_key"] == "herg_ic50"
    assert captured["adapter_builder"]["endpoint"].endpoint_key == "herg_ic50"
    assert captured["adapter_builder"]["source_name"] == "chembl"
    assert captured["adapter_builder"]["source_config"]["target_chembl_id"] == "CHEMBL240"
    assert captured["adapter_builder"]["http_config"].request_timeout_seconds == 9
    assert captured["adapter_builder"]["http_config"].http_retries == 1
    assert captured["pipeline"]["endpoint_key"] == "herg_ic50"
    assert captured["pipeline"]["run_config"].dry_run is True
    assert captured["pipeline"]["run_config"].max_records == 25
    assert captured["pipeline"]["run_config"].commit_every == 10
    assert captured["pipeline"]["run_config"].fail_fast is True
    assert result.endpoint_key == "herg_ic50"
    assert result.source_name == "chembl"
    assert result.processed == 4
    assert result.stored == 3
    assert result.skipped_invalid == 1
    assert result.warnings == 2
    assert result.duration_seconds == 1.25
    assert result.ingestion_run_id == 123
