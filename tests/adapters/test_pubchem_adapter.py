import json
from pathlib import Path

import pytest

from herg.config import HttpConfig
from herg.sources.pubchem import PubChemAdapter


FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "herg_ic50"


def _load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


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
