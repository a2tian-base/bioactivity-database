import json
from decimal import Decimal
from pathlib import Path

import pytest

from bioactivity.endpoints import EndpointConfig, get_source_config
from herg.config import HttpConfig
from herg.sources.pubchem import PubChemAdapter, measurement_input_from_pubchem_record


FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "herg_ic50"


def _load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _herg_ic50_endpoint() -> EndpointConfig:
    return EndpointConfig(
        endpoint_id=1,
        endpoint_key="herg_ic50",
        display_name="hERG IC50",
        spec={
            "measurement": {
                "type": "IC50",
                "value_kind": "concentration",
            }
        },
        source_configs={
            "pubchem": {
                "target_gene_symbol": "KCNH2",
                "target_gene_id": "3757",
                "activity_name_regex": r"(?i)\bic50\b",
            }
        },
        spec_hash="fixture",
        active=True,
    )


def _build_adapter(monkeypatch, concise_rows: list[dict]) -> PubChemAdapter:
    metadata_payload = _load_json("pubchem_cid_properties.json")

    def fake_get_csv_rows(url, config, label="PubChem"):
        for row in concise_rows:
            yield row

    def fake_get_json(url, params, config, label="PubChem"):
        return metadata_payload

    monkeypatch.setattr("herg.sources.pubchem.get_csv_rows", fake_get_csv_rows)
    monkeypatch.setattr("herg.sources.pubchem.get_json", fake_get_json)

    return PubChemAdapter(
        http_config=HttpConfig(request_timeout_seconds=1, http_retries=0),
        base_url="https://example.org",
        target_gene_symbol="KCNH2",
        target_gene_id="3757",
        activity_name_regex=r"(?i)\bic50\b",
        cid_batch_size=100,
    )


def test_pubchem_adapter_explicit_parameters_expose_existing_effective_config(monkeypatch):
    adapter = _build_adapter(monkeypatch, concise_rows=[])

    assert adapter.effective_config == {
        "target_gene_symbol": "KCNH2",
        "target_gene_id": "3757",
        "activity_name_regex": r"(?i)\bic50\b",
        "cid_batch_size": 100,
    }


def test_pubchem_adapter_from_source_config_matches_explicit_herg_config(monkeypatch):
    explicit_adapter = _build_adapter(monkeypatch, concise_rows=[])
    endpoint = _herg_ic50_endpoint()

    configured_adapter = PubChemAdapter.from_source_config(
        endpoint,
        get_source_config(endpoint, "pubchem"),
        http_config=HttpConfig(request_timeout_seconds=1, http_retries=0),
        base_url="https://example.org",
        cid_batch_size=100,
    )

    assert configured_adapter.effective_config == explicit_adapter.effective_config


def test_pubchem_adapter_from_source_config_rejects_missing_target_gene_symbol():
    endpoint = _herg_ic50_endpoint()
    source_config = get_source_config(endpoint, "pubchem")
    source_config.pop("target_gene_symbol")

    with pytest.raises(ValueError, match="target_gene_symbol"):
        PubChemAdapter.from_source_config(
            endpoint,
            source_config,
            http_config=HttpConfig(request_timeout_seconds=1, http_retries=0),
        )


def test_pubchem_adapter_from_source_config_rejects_missing_target_gene_id():
    endpoint = _herg_ic50_endpoint()
    source_config = get_source_config(endpoint, "pubchem")
    source_config.pop("target_gene_id")

    with pytest.raises(ValueError, match="target_gene_id"):
        PubChemAdapter.from_source_config(
            endpoint,
            source_config,
            http_config=HttpConfig(request_timeout_seconds=1, http_retries=0),
        )


def test_pubchem_adapter_from_source_config_rejects_missing_activity_name_regex():
    endpoint = _herg_ic50_endpoint()
    source_config = get_source_config(endpoint, "pubchem")
    source_config.pop("activity_name_regex")

    with pytest.raises(ValueError, match="activity_name_regex"):
        PubChemAdapter.from_source_config(
            endpoint,
            source_config,
            http_config=HttpConfig(request_timeout_seconds=1, http_retries=0),
        )


def test_pubchem_adapter_from_source_config_rejects_invalid_activity_name_regex():
    endpoint = _herg_ic50_endpoint()
    source_config = get_source_config(endpoint, "pubchem")
    source_config["activity_name_regex"] = "("

    with pytest.raises(ValueError, match="activity_name_regex"):
        PubChemAdapter.from_source_config(
            endpoint,
            source_config,
            http_config=HttpConfig(request_timeout_seconds=1, http_retries=0),
        )


def test_pubchem_adapter_maps_and_filters_herg_ic50_fixtures(monkeypatch):
    adapter = _build_adapter(
        monkeypatch,
        concise_rows=[
            _load_json("pubchem_concise_ic50_equal.json"),
            _load_json("pubchem_concise_wrong_gene.json"),
            _load_json("pubchem_concise_wrong_activity_name.json"),
            _load_json("pubchem_concise_missing_activity_value.json"),
        ],
    )

    raw_rows = list(adapter.iter_raw_rows())
    assert [row["external_key"] for row in raw_rows] == ["aid:1|sid:2|cid:3"]

    enriched = adapter.enrich_batch(raw_rows)
    staged = adapter.map_row(enriched[0])

    assert staged.source_record.source_record_key == "aid:1|sid:2|cid:3"
    assert staged.compound.standard_inchikey == "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
    assert staged.measurement.ic50_value == 0.85
    assert staged.measurement.ic50_unit == "uM"
    assert staged.measurement.qualifier == "="
    assert staged.measurement.endpoint == "IC50"

    generic_measurement = measurement_input_from_pubchem_record(staged)
    assert generic_measurement.result_key == "aid:1|sid:2|cid:3"
    assert generic_measurement.measurement_type == "IC50"
    assert generic_measurement.value_kind == "concentration"
    assert generic_measurement.original_value == Decimal("0.85")
    assert generic_measurement.original_unit == "uM"
    assert generic_measurement.original_relation == "="
    assert generic_measurement.assay_context == {
        "aid": "1",
        "sid": "2",
        "cid": "3",
        "activity_name": "IC50",
        "activity_outcome": "Active",
        "assay_name": "Test Assay",
    }
    assert generic_measurement.quality_flags == {"source": "pubchem"}


def test_pubchem_adapter_rejects_missing_mapped_value(monkeypatch):
    adapter = _build_adapter(monkeypatch, concise_rows=[])

    with pytest.raises(ValueError, match="Missing IC50 value"):
        adapter.map_row(
            {
                "aid": 1,
                "sid": 2,
                "cid": 3,
                "ic50_unit": "uM",
                "cid_metadata": {},
            }
        )
