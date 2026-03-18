import json
from pathlib import Path

from herg.config import DbConfig, HttpConfig
from herg.sources.pubchem_structures import PubChemStructureAdapter


FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_pubchem_structure_adapter_maps_fixture(monkeypatch):
    metadata_payload = _load_fixture("pubchem_cid_properties.json")
    candidate_rows = [
        {
            "compound_id": 202,
            "standard_inchikey": "",
            "chembl_id": "CHEMBL25",
            "pubchem_cid": 3,
            "unii": "",
            "preferred_name": "Seed Name",
            "canonical_smiles": "CCO",
            "standard_inchi": "",
        }
    ]

    monkeypatch.setattr(
        "herg.sources.pubchem_structures.fetch_structure_enrichment_candidates",
        lambda provider, limit=None, db_config=None: candidate_rows,
    )
    monkeypatch.setattr(
        "herg.sources.pubchem_structures.get_json",
        lambda url, params, config, label="PubChem": metadata_payload,
    )

    adapter = PubChemStructureAdapter(
        http_config=HttpConfig(request_timeout_seconds=1, http_retries=0),
        db_config=DbConfig(host="localhost", port=5432, dbname="herg", user="herg_user", password="change_me"),
        base_url="https://example.org",
        limit=10,
        cid_batch_size=50,
    )

    raw_rows = list(adapter.iter_raw_rows())
    assert len(raw_rows) == 1

    enriched = adapter.enrich_batch(raw_rows)
    record = adapter.map_row(enriched[0])

    assert record.external_key == "compound:202|provider:pubchem:structure"
    assert record.match.identifiers[0].namespace == "pubchem_cid"
    assert record.structure.canonical_smiles == ""
    assert record.structure.standard_inchi == "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3"
    assert record.structure.standard_inchikey == "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
    assert record.source_record.source_name == "pubchem"
    assert record.source_record.source_record_key == "cid:3|properties:structure"
