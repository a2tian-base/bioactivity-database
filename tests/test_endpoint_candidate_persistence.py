from __future__ import annotations

import pytest

from bioactivity.endpoints import (
    DuplicateEndpointKeyError,
    endpoint_key_from_candidate,
    endpoint_spec_hash,
    load_endpoint,
    save_endpoint_candidate,
)
from bioactivity.endpoint_search import parse_endpoint_query
from bioactivity.source_discovery import EndpointCandidate, SourceAvailability
from bioactivity.sources.chembl_discovery import build_chembl_endpoint_candidates, search_chembl_targets


class _EndpointTableCursor:
    def __init__(self):
        self.rows: list[dict[str, object]] = []
        self._next_one: tuple[object, ...] | None = None
        self._next_all: list[tuple[object, ...]] = []

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        normalized_sql = " ".join(sql.lower().split())
        params = params or ()
        self._next_one = None
        self._next_all = []

        if normalized_sql.startswith("select") and "from endpoints" in normalized_sql:
            if "where spec_hash = %s" in normalized_sql:
                self._next_one = self._row_by("spec_hash", params[0])
            elif "where endpoint_key = %s" in normalized_sql:
                self._next_one = self._row_by("endpoint_key", params[0])
            return

        if normalized_sql.startswith("insert into endpoints"):
            endpoint_id = len(self.rows) + 1
            spec = getattr(params[2], "obj", params[2])
            source_configs = getattr(params[3], "obj", params[3])
            row = {
                "endpoint_id": endpoint_id,
                "endpoint_key": params[0],
                "display_name": params[1],
                "spec": spec,
                "source_configs": source_configs,
                "spec_hash": params[4],
                "active": params[5],
            }
            self.rows.append(row)
            self._next_one = self._row_tuple(row)
            return

        raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchone(self):
        return self._next_one

    def fetchall(self):
        return list(self._next_all)

    def _row_by(self, field_name: str, value: object) -> tuple[object, ...] | None:
        for row in self.rows:
            if row[field_name] == value:
                return self._row_tuple(row)
        return None

    @staticmethod
    def _row_tuple(row: dict[str, object]) -> tuple[object, ...]:
        return (
            row["endpoint_id"],
            row["endpoint_key"],
            row["display_name"],
            row["spec"],
            row["source_configs"],
            row["spec_hash"],
            row["active"],
        )


def _candidate(
    gene_symbol: str,
    measurement_type: str,
    *,
    display_name: str | None = None,
    value_kind: str = "concentration",
    target_chembl_id: str = "CHEMBL203",
) -> EndpointCandidate:
    return EndpointCandidate(
        candidate_key=f"chembl_{target_chembl_id.lower()}_{measurement_type.lower()}",
        display_name=display_name or f"{gene_symbol} {measurement_type}",
        template_key="concentration_potency",
        spec={
            "target": {
                "preferred_name": gene_symbol,
                "gene_symbol": gene_symbol,
                "organism": "Homo sapiens",
                "identifiers": {
                    "chembl_target_id": target_chembl_id,
                },
            },
            "measurement": {
                "type": measurement_type,
                "value_kind": value_kind,
            },
        },
        source_configs={
            "chembl": {
                "target_chembl_id": target_chembl_id,
                "standard_type": measurement_type,
            }
        },
        source_availability=(
            SourceAvailability(
                source_name="chembl",
                source_target_id=target_chembl_id,
                measurement_type=measurement_type,
                approximate_count=12,
            ),
        ),
        warnings=(),
        score=100.0,
    )


def _chembl_target_payload() -> dict[str, object]:
    return {
        "target_chembl_id": "CHEMBL203",
        "pref_name": "Epidermal growth factor receptor",
        "organism": "Homo sapiens",
        "target_type": "SINGLE PROTEIN",
        "target_components": [
            {
                "accession": "P00533",
                "target_component_synonyms": [
                    {
                        "component_synonym": "EGFR",
                        "syn_type": "GENE_SYMBOL",
                    }
                ],
            }
        ],
        "cross_references": [],
    }


@pytest.mark.parametrize(
    ("candidate", "endpoint_key"),
    [
        (_candidate("hERG", "IC50"), "herg_ic50"),
        (_candidate("EGFR", "IC50"), "egfr_ic50"),
        (_candidate("DRD2", "Ki"), "drd2_ki"),
        (_candidate("CYP3A4", "inhibition", value_kind="percent"), "cyp3a4_inhibition"),
    ],
)
def test_endpoint_key_from_candidate_uses_target_and_measurement_slugs(candidate, endpoint_key):
    assert endpoint_key_from_candidate(candidate) == endpoint_key


def test_save_endpoint_candidate_inserts_valid_endpoint_row():
    cur = _EndpointTableCursor()
    candidate = _candidate("EGFR", "IC50")

    endpoint = save_endpoint_candidate(cur, candidate)

    assert endpoint.endpoint_id == 1
    assert endpoint.endpoint_key == "egfr_ic50"
    assert endpoint.display_name == "EGFR IC50"
    assert endpoint.spec["measurement"]["type"] == "IC50"
    assert endpoint.source_config("chembl")["target_chembl_id"] == "CHEMBL203"
    assert cur.rows[0]["endpoint_key"] == "egfr_ic50"


def test_save_endpoint_candidate_accepts_mocked_chembl_discovery_candidate(monkeypatch):
    def fake_get_json(url, params, config, label="ChEMBL"):
        if url.endswith("/target/search.json"):
            return {"targets": [_chembl_target_payload()]}
        if url.endswith("/activity.json"):
            return {"page_meta": {"total_count": 24585}, "activities": []}
        raise AssertionError(f"Unexpected ChEMBL URL: {url}")

    monkeypatch.setattr("bioactivity.sources.chembl_discovery.get_json", fake_get_json)
    query = parse_endpoint_query("EGFR IC50")
    candidate = build_chembl_endpoint_candidates(query, search_chembl_targets(query))[0]

    endpoint = save_endpoint_candidate(_EndpointTableCursor(), candidate)

    assert endpoint.endpoint_key == "egfr_ic50"
    assert endpoint.source_config("chembl")["target_chembl_id"] == "CHEMBL203"


def test_saved_candidate_can_be_loaded_by_existing_endpoint_loader():
    cur = _EndpointTableCursor()
    saved = save_endpoint_candidate(cur, _candidate("EGFR", "IC50"))

    loaded = load_endpoint(cur, saved.endpoint_key)

    assert loaded == saved


def test_save_endpoint_candidate_uses_stable_spec_hash():
    cur = _EndpointTableCursor()
    candidate = _candidate("EGFR", "IC50")

    endpoint = save_endpoint_candidate(cur, candidate)
    expected_hash = endpoint_spec_hash(
        endpoint_key="egfr_ic50",
        display_name="EGFR IC50",
        spec=candidate.spec,
        source_configs=candidate.source_configs,
    )

    assert endpoint.spec_hash == expected_hash
    assert endpoint.spec_hash == endpoint_spec_hash(
        endpoint_key="egfr_ic50",
        display_name="EGFR IC50",
        spec=dict(candidate.spec),
        source_configs=dict(candidate.source_configs),
    )


def test_save_endpoint_candidate_returns_existing_endpoint_for_same_candidate():
    cur = _EndpointTableCursor()
    candidate = _candidate("EGFR", "IC50")

    first = save_endpoint_candidate(cur, candidate)
    second = save_endpoint_candidate(cur, candidate)

    assert second == first
    assert len(cur.rows) == 1


def test_save_endpoint_candidate_duplicate_key_with_different_spec_raises_clear_error():
    cur = _EndpointTableCursor()
    save_endpoint_candidate(cur, _candidate("EGFR", "IC50", target_chembl_id="CHEMBL203"))

    with pytest.raises(DuplicateEndpointKeyError, match="already exists with a different endpoint specification"):
        save_endpoint_candidate(
            cur,
            _candidate("EGFR", "IC50", target_chembl_id="CHEMBL999999"),
            endpoint_key="egfr_ic50",
        )
