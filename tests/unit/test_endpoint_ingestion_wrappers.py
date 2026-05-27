import importlib.util
from pathlib import Path
from types import SimpleNamespace

from bioactivity.endpoints import EndpointConfig
from bioactivity.ingest import run_endpoint_ingestion
from herg.config import DbConfig, HttpConfig, RunConfig
from herg.sources import chembl, pubchem


ROOT = Path(__file__).resolve().parents[2]


class _DummyConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _endpoint(endpoint_key: str = "herg_ic50") -> EndpointConfig:
    return EndpointConfig(
        endpoint_id=1,
        endpoint_key=endpoint_key,
        display_name="hERG IC50",
        spec={"measurement": {"type": "IC50", "value_kind": "concentration"}},
        source_configs={
            "chembl": {
                "target_chembl_id": "CHEMBL240",
                "standard_type": "IC50",
            },
            "pubchem": {
                "target_gene_symbol": "KCNH2",
                "target_gene_id": "3757",
                "activity_name_regex": r"(?i)\bic50\b",
            },
        },
        spec_hash="fixture",
        active=True,
    )


def _load_script(script_name: str):
    path = ROOT / "app" / "scripts" / script_name
    spec = importlib.util.spec_from_file_location(script_name.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_run_endpoint_ingestion_loads_endpoint_config_and_calls_pipeline(monkeypatch):
    captured = {}

    def fake_get_conn(*, db_config):
        captured["db_config_for_conn"] = db_config
        return _DummyConnection()

    def fake_load_endpoint(conn, endpoint_key):
        captured["loaded_endpoint_key"] = endpoint_key
        return _endpoint(endpoint_key)

    def fake_factory(endpoint, source_config, http_config, options):
        captured["factory"] = {
            "endpoint_key": endpoint.endpoint_key,
            "source_config": source_config,
            "http_config": http_config,
            "options": options,
        }
        return SimpleNamespace(source_name="chembl")

    def fake_pipeline(adapter, db_config, run_config, *, endpoint_key):
        captured["pipeline"] = {
            "adapter": adapter,
            "db_config": db_config,
            "run_config": run_config,
            "endpoint_key": endpoint_key,
        }
        return SimpleNamespace(processed=1, stored=1, skipped_invalid=0, failed=0)

    monkeypatch.setattr("bioactivity.ingest.get_conn", fake_get_conn)
    monkeypatch.setattr("bioactivity.ingest.load_endpoint", fake_load_endpoint)
    db_config = DbConfig(host="localhost", port=5432, dbname="herg", user="herg_user", password="change_me")
    http_config = HttpConfig(request_timeout_seconds=1, http_retries=0)
    run_config = RunConfig(dry_run=True, max_records=3)

    stats = run_endpoint_ingestion(
        endpoint_key="herg_ic50",
        source_name="chembl",
        db_config=db_config,
        http_config=http_config,
        run_config=run_config,
        source_config_overrides={"target_chembl_id": "CHEMBL999"},
        activity_page_size=10,
        adapter_factories={"chembl": fake_factory},
        pipeline_runner=fake_pipeline,
    )

    assert stats.stored == 1
    assert captured["db_config_for_conn"] == db_config
    assert captured["loaded_endpoint_key"] == "herg_ic50"
    assert captured["factory"]["endpoint_key"] == "herg_ic50"
    assert captured["factory"]["source_config"]["target_chembl_id"] == "CHEMBL999"
    assert captured["factory"]["source_config"]["standard_type"] == "IC50"
    assert captured["factory"]["options"]["activity_page_size"] == 10
    assert captured["pipeline"]["endpoint_key"] == "herg_ic50"
    assert captured["pipeline"]["db_config"] == db_config
    assert captured["pipeline"]["run_config"] == run_config


def test_old_chembl_script_exports_compatibility_main():
    script = _load_script("ingest_chembl_herg.py")

    assert script.main is chembl.main


def test_old_pubchem_script_exports_compatibility_main():
    script = _load_script("ingest_pubchem_herg.py")

    assert script.main is pubchem.main


def test_chembl_herg_main_delegates_to_endpoint_ingestion(monkeypatch):
    captured = {}

    def fake_run_endpoint_ingestion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(processed=2, stored=2, skipped_invalid=0, failed=0)

    monkeypatch.setattr("herg.sources.chembl.run_endpoint_ingestion", fake_run_endpoint_ingestion)
    monkeypatch.setattr(
        "sys.argv",
        [
            "ingest_chembl_herg.py",
            "--dry-run",
            "--max-records",
            "2",
            "--target-chembl-id",
            "CHEMBL240",
            "--chembl-base-url",
            "https://example.org/chembl",
        ],
    )

    assert chembl.main() == 0
    assert captured["endpoint_key"] == "herg_ic50"
    assert captured["source_name"] == "chembl"
    assert captured["source_config_overrides"]["target_chembl_id"] == "CHEMBL240"
    assert captured["chembl_base_url"] == "https://example.org/chembl"
    assert captured["run_config"].dry_run is True
    assert captured["run_config"].max_records == 2


def test_pubchem_herg_main_delegates_to_endpoint_ingestion(monkeypatch):
    captured = {}

    def fake_run_endpoint_ingestion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(processed=3, stored=3, skipped_invalid=0, failed=0)

    monkeypatch.setattr("herg.sources.pubchem.run_endpoint_ingestion", fake_run_endpoint_ingestion)
    monkeypatch.setattr(
        "sys.argv",
        [
            "ingest_pubchem_herg.py",
            "--dry-run",
            "--max-records",
            "3",
            "--target-gene-symbol",
            "KCNH2",
            "--target-gene-id",
            "3757",
            "--pubchem-base-url",
            "https://example.org/pubchem",
        ],
    )

    assert pubchem.main() == 0
    assert captured["endpoint_key"] == "herg_ic50"
    assert captured["source_name"] == "pubchem"
    assert captured["source_config_overrides"]["target_gene_symbol"] == "KCNH2"
    assert captured["source_config_overrides"]["target_gene_id"] == "3757"
    assert captured["pubchem_base_url"] == "https://example.org/pubchem"
    assert captured["run_config"].dry_run is True
    assert captured["run_config"].max_records == 3
