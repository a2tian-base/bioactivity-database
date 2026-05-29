import pytest

from bioactivity.endpoints import EndpointConfig
from bioactivity.source_adapters import build_source_adapter
from herg.config import HttpConfig


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
                "standard_relation__in": ["=", "<", ">"],
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


def test_source_adapter_factory_builds_chembl_from_endpoint_config(monkeypatch):
    def fake_get_json(url, params, config, label="ChEMBL"):
        assert url.endswith("status.json")
        return {"chembl_db_version": "fixture"}

    monkeypatch.setattr("herg.sources.chembl.get_json", fake_get_json)
    adapter = build_source_adapter(
        endpoint=_endpoint(),
        source_name="ChEMBL",
        source_config=_endpoint().source_config("chembl"),
        http_config=HttpConfig(request_timeout_seconds=1, http_retries=0),
        chembl_base_url="https://example.org/chembl",
        activity_page_size=10,
        molecule_batch_size=5,
    )

    assert adapter.source_name == "chembl"
    assert adapter.target_chembl_id == "CHEMBL240"
    assert adapter.standard_type == "IC50"
    assert adapter.activity_page_size == 10
    assert adapter.molecule_batch_size == 5


def test_source_adapter_factory_builds_pubchem_from_endpoint_config():
    adapter = build_source_adapter(
        endpoint=_endpoint(),
        source_name="PubChem",
        source_config=_endpoint().source_config("pubchem"),
        http_config=HttpConfig(request_timeout_seconds=1, http_retries=0),
        pubchem_base_url="https://example.org/pubchem",
        cid_batch_size=7,
    )

    assert adapter.source_name == "pubchem"
    assert adapter.target_gene_symbol == "KCNH2"
    assert adapter.target_gene_id == "3757"
    assert adapter.cid_batch_size == 7


def test_source_adapter_factory_rejects_unsupported_source():
    with pytest.raises(ValueError, match="Unsupported source 'unichem'"):
        build_source_adapter(
            endpoint=_endpoint(),
            source_name="UniChem",
            source_config={},
            http_config=HttpConfig(request_timeout_seconds=1, http_retries=0),
        )
