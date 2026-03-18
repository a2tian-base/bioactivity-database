import json
from pathlib import Path

from herg.config import DbConfig, HttpConfig
from herg.sources.chembl_structures import ChemblStructureAdapter


FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_chembl_structure_adapter_maps_fixture(monkeypatch):
    molecule_payload = _load_fixture("chembl_molecule.json")
    candidate_rows = [
        {
            "compound_id": 101,
            "standard_inchikey": "",
            "chembl_id": "CHEMBL25",
            "pubchem_cid": 702,
            "unii": "3K9958V90M",
            "preferred_name": "Seed Name",
            "canonical_smiles": "",
            "standard_inchi": "",
        }
    ]

    def fake_get_json(url, params, config, label="ChEMBL"):
        if url.endswith("status.json"):
            return {"chembl_db_version": "CHEMBL-TEST"}
        if url.endswith("molecule.json"):
            return molecule_payload
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("herg.sources.chembl_structures.get_json", fake_get_json)
    monkeypatch.setattr(
        "herg.sources.chembl_structures.fetch_structure_enrichment_candidates",
        lambda provider, limit=None, db_config=None: candidate_rows,
    )

    adapter = ChemblStructureAdapter(
        http_config=HttpConfig(request_timeout_seconds=1, http_retries=0),
        db_config=DbConfig(host="localhost", port=5432, dbname="herg", user="herg_user", password="change_me"),
        base_url="https://example.org",
        limit=10,
        molecule_batch_size=50,
    )

    raw_rows = list(adapter.iter_raw_rows())
    assert len(raw_rows) == 1

    enriched = adapter.enrich_batch(raw_rows)
    record = adapter.map_row(enriched[0])

    assert record.external_key == "compound:101|provider:chembl:structure"
    assert record.match.identifiers[0].namespace == "chembl_id"
    assert record.structure.canonical_smiles == "CCO"
    assert record.structure.standard_inchi == "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3"
    assert record.structure.standard_inchikey == "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
    assert record.source_record.source_name == "chembl"
    assert record.source_record.source_record_key == "molecule:CHEMBL25"
    assert record.source_record.source_release == "CHEMBL-TEST"
