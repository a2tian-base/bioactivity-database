import csv
import json
from pathlib import Path

from herg.config import HttpConfig
from herg.sources.pubchem import PubChemAdapter


FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def _load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _load_csv_rows(name: str) -> list[dict]:
    path = FIXTURES / name
    with path.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader)


def test_pubchem_adapter_maps_fixture(monkeypatch):
    concise_rows = _load_csv_rows("pubchem_concise.csv")
    metadata_payload = _load_json("pubchem_cid_properties.json")

    def fake_get_csv_rows(url, config, label="PubChem"):
        for row in concise_rows:
            yield row

    def fake_get_json(url, params, config, label="PubChem"):
        return metadata_payload

    monkeypatch.setattr("herg.sources.pubchem.get_csv_rows", fake_get_csv_rows)
    monkeypatch.setattr("herg.sources.pubchem.get_json", fake_get_json)

    adapter = PubChemAdapter(
        http_config=HttpConfig(request_timeout_seconds=1, http_retries=0),
        base_url="https://example.org",
        target_gene_symbol="KCNH2",
        target_gene_id="3757",
        activity_name_regex=r"(?i)\bic50\b",
        cid_batch_size=100,
    )

    raw_rows = list(adapter.iter_raw_rows())
    assert len(raw_rows) == 1

    enriched = adapter.enrich_batch(raw_rows)
    staged = adapter.map_row(enriched[0])

    assert staged.source_record.source_record_key == "aid:1|sid:2|cid:3"
    assert staged.compound.standard_inchikey == "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
    assert staged.measurement.ic50_unit == "uM"
