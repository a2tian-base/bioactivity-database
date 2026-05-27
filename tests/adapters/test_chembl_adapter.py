import json
from pathlib import Path

import pytest

from herg.config import HttpConfig
from herg.sources.chembl import ChemblAdapter


FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "herg_ic50"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _build_adapter(monkeypatch, activities: list[dict], molecules: list[dict]) -> ChemblAdapter:
    activity_payload = {
        "page_meta": {"total_count": len(activities)},
        "activities": activities,
    }
    molecule_payload = {"molecules": molecules}

    def fake_get_json(url, params, config, label="ChEMBL"):
        if url.endswith("status.json"):
            return {"chembl_db_version": "v1"}
        if "activity.json" in url:
            return activity_payload
        if "molecule.json" in url:
            return molecule_payload
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("herg.sources.chembl.get_json", fake_get_json)

    return ChemblAdapter(
        http_config=HttpConfig(request_timeout_seconds=1, http_retries=0),
        base_url="https://example.org",
        target_chembl_id="CHEMBL240",
        standard_type="IC50",
        relations="=,<,>",
        activity_page_size=1000,
        molecule_batch_size=50,
    )


def test_chembl_adapter_maps_equal_and_inequality_fixtures(monkeypatch):
    molecule = _load_fixture("chembl_molecule_chembl25.json")
    adapter = _build_adapter(
        monkeypatch,
        activities=[
            _load_fixture("chembl_activity_ic50_equal.json"),
            _load_fixture("chembl_activity_ic50_less_than.json"),
        ],
        molecules=[molecule],
    )

    raw_rows = list(adapter.iter_raw_rows())
    assert [row["external_key"] for row in raw_rows] == ["activity:123", "activity:124"]

    enriched = adapter.enrich_batch(raw_rows)
    staged_by_key = {row["external_key"]: adapter.map_row(row) for row in enriched}

    equal = staged_by_key["activity:123"]
    assert equal.compound.standard_inchikey == "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
    assert equal.source_record.source_record_key == "activity:123"
    assert equal.source_record.source_release == "v1"
    assert equal.measurement.ic50_value == 50.0
    assert equal.measurement.ic50_unit == "nM"
    assert equal.measurement.qualifier == "="
    assert equal.measurement.endpoint == "IC50"

    less_than = staged_by_key["activity:124"]
    assert less_than.source_record.source_record_key == "activity:124"
    assert less_than.measurement.ic50_value == 25.0
    assert less_than.measurement.ic50_unit == "nM"
    assert less_than.measurement.qualifier == "<"
    assert less_than.measurement.endpoint == "IC50"


def test_chembl_adapter_rejects_missing_measurement_value(monkeypatch):
    molecule = _load_fixture("chembl_molecule_chembl25.json")
    adapter = _build_adapter(
        monkeypatch,
        activities=[],
        molecules=[molecule],
    )

    with pytest.raises(ValueError, match="Expected positive number"):
        adapter.map_row(
            {
                "activity": _load_fixture("chembl_activity_missing_value.json"),
                "molecule": molecule,
                "source_release": "v1",
            }
        )
