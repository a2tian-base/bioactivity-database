import pytest

from herg.config import DbConfig
from herg.db import StructureConflictError, compute_structure_enrichment_delta
from herg.models import StructureInput
from herg.read_db import fetch_structure_enrichment_candidates


class _CapturingCursor:
    def __init__(self, queries):
        self.queries = queries

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=()):
        self.queries.append((query, params))

    def fetchall(self):
        return []


class _CapturingConnection:
    def __init__(self, queries):
        self.queries = queries

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self, row_factory=None):
        return _CapturingCursor(self.queries)


def test_compute_structure_enrichment_delta_treats_smiles_difference_as_soft():
    current = {
        "compound_id": 1,
        "canonical_smiles": "CCC",
        "standard_inchi": "",
        "standard_inchikey": "",
    }
    structure = StructureInput(
        canonical_smiles="CCO",
        standard_inchi="InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3",
        standard_inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
    )

    delta = compute_structure_enrichment_delta(current, structure)

    assert delta["added_fields"] == ("standard_inchi", "standard_inchikey")
    assert delta["soft_differences"] == ("canonical_smiles",)


def test_compute_structure_enrichment_delta_raises_on_inchikey_conflict():
    current = {
        "compound_id": 1,
        "canonical_smiles": "",
        "standard_inchi": "",
        "standard_inchikey": "AAAAAAAABBBBBB-UHFFFAOYSA-N",
    }
    structure = StructureInput(standard_inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N")

    with pytest.raises(StructureConflictError):
        compute_structure_enrichment_delta(current, structure)


@pytest.mark.parametrize(
    ("provider", "expected_sql"),
    [
        ("chembl", "chembl_id IS NOT NULL"),
        ("pubchem", "pubchem_cid IS NOT NULL"),
    ],
)
def test_fetch_structure_enrichment_candidates_uses_provider_filter(monkeypatch, provider, expected_sql):
    queries = []
    monkeypatch.setattr("herg.read_db.get_conn", lambda db_config=None: _CapturingConnection(queries))

    rows = fetch_structure_enrichment_candidates(
        provider,
        limit=25,
        db_config=DbConfig(host="localhost", port=5432, dbname="herg", user="herg_user", password="change_me"),
    )

    assert rows == []
    assert len(queries) == 1
    assert expected_sql in queries[0][0]
    assert queries[0][1] == (25,)
