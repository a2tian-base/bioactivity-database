import json
from pathlib import Path

from herg.config import HttpConfig
from herg.sources.chembl import ChemblAdapter


FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_chembl_adapter_maps_fixture(monkeypatch):
    activity_payload = _load_fixture("chembl_activity.json")
    molecule_payload = _load_fixture("chembl_molecule.json")

    def fake_get_json(url, params, config, label="ChEMBL"):
        if url.endswith("status.json"):
            return {"chembl_db_version": "v1"}
        if "activity.json" in url:
            return activity_payload
        if "molecule.json" in url:
            return molecule_payload
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("herg.sources.chembl.get_json", fake_get_json)

    adapter = ChemblAdapter(
        http_config=HttpConfig(request_timeout_seconds=1, http_retries=0),
        base_url="https://example.org",
        target_chembl_id="CHEMBL240",
        standard_type="IC50",
        relations="=,<,>",
        activity_page_size=1000,
        molecule_batch_size=50,
    )

    raw_rows = list(adapter.iter_raw_rows())
    assert len(raw_rows) == 1

    enriched = adapter.enrich_batch(raw_rows)
    staged = adapter.map_row(enriched[0])

    assert staged.external_key == "activity:123"
    assert staged.compound.standard_inchikey == "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
    assert staged.source_record.source_record_key == "activity:123"
    assert staged.source_record.source_release == "v1"
    assert staged.measurement.ic50_value == 50.0
