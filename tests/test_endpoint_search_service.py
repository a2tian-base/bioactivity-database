from __future__ import annotations

import urllib.request

import pytest

from bioactivity.endpoint_search import search_endpoints
from bioactivity.source_discovery import EndpointCandidate, SourceAvailability, SourceDiscoveryError, SourceDiscoveryResult


class _SearchCursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self._next_all = []

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        normalized_sql = " ".join(sql.lower().split())
        if normalized_sql.startswith("select") and "from endpoints" in normalized_sql:
            include_inactive = bool((params or (False,))[0])
            self._next_all = [row for row in self._rows if include_inactive or row[5]]
            return
        raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchall(self):
        return list(self._next_all)


def _saved_endpoint_rows():
    return [
        (
            1,
            "egfr_ic50",
            "EGFR IC50",
            {
                "target": {
                    "preferred_name": "Epidermal growth factor receptor",
                    "gene_symbol": "EGFR",
                    "organism": "Homo sapiens",
                },
                "measurement": {
                    "type": "IC50",
                    "value_kind": "concentration",
                },
            },
            {
                "chembl": {
                    "target_chembl_id": "CHEMBL203",
                    "standard_type": "IC50",
                }
            },
            True,
        )
    ]


def _candidate() -> EndpointCandidate:
    return EndpointCandidate(
        candidate_key="chembl_chembl999999_ic50",
        display_name="EGFR reference IC50",
        template_key="concentration_potency",
        spec={
            "target": {
                "preferred_name": "EGFR reference target",
                "gene_symbol": "EGFR",
                "organism": "Homo sapiens",
            },
            "measurement": {
                "type": "IC50",
                "value_kind": "concentration",
            },
        },
        source_configs={
            "chembl": {
                "target_chembl_id": "CHEMBL999999",
                "standard_type": "IC50",
            }
        },
        source_availability=(
            SourceAvailability(
                source_name="chembl",
                source_target_id="CHEMBL999999",
                measurement_type="IC50",
                approximate_count=25,
            ),
        ),
        warnings=(),
        score=85.0,
    )


def test_search_endpoints_returns_saved_endpoints_and_candidates_separately(monkeypatch):
    candidate = _candidate()

    def fake_discover(query, *, sources, limit):
        assert query.target_query == "EGFR"
        assert query.measurement_types == ("IC50",)
        assert sources == ("chembl",)
        assert limit == 10
        return SourceDiscoveryResult(candidates=(candidate,), warnings=(), failed_sources=())

    monkeypatch.setattr("bioactivity.endpoint_search.discover_endpoint_candidates", fake_discover)

    result = search_endpoints(_SearchCursor(_saved_endpoint_rows()), "EGFR IC50")

    assert [endpoint.endpoint_key for endpoint in result.saved_endpoints] == ["egfr_ic50"]
    assert result.candidates == (candidate,)
    assert result.warnings == ()


def test_search_endpoints_source_failure_becomes_warning_without_breaking_saved_search(monkeypatch):
    def fake_discover(query, *, sources, limit):
        raise RuntimeError("mock ChEMBL outage")

    monkeypatch.setattr("bioactivity.endpoint_search.discover_endpoint_candidates", fake_discover)

    result = search_endpoints(_SearchCursor(_saved_endpoint_rows()), "EGFR IC50")

    assert [endpoint.endpoint_key for endpoint in result.saved_endpoints] == ["egfr_ic50"]
    assert result.candidates == ()
    assert result.warnings == ("Endpoint discovery failed: mock ChEMBL outage",)


def test_search_endpoints_raises_when_all_sources_fail_and_no_saved_endpoints(monkeypatch):
    def fake_discover(query, *, sources, limit):
        return SourceDiscoveryResult(
            candidates=(),
            warnings=("chembl endpoint discovery failed: mock ChEMBL outage",),
            failed_sources=("chembl",),
        )

    monkeypatch.setattr("bioactivity.endpoint_search.discover_endpoint_candidates", fake_discover)

    with pytest.raises(SourceDiscoveryError, match="mock ChEMBL outage"):
        search_endpoints(_SearchCursor([]), "EGFR IC50")


def test_search_endpoints_does_not_use_live_http_in_unit_tests(monkeypatch):
    def fail_urlopen(*args, **kwargs):
        raise AssertionError("Live HTTP should not be used in endpoint search service tests.")

    def fake_discover(query, *, sources, limit):
        return SourceDiscoveryResult(candidates=(_candidate(),), warnings=(), failed_sources=())

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    monkeypatch.setattr("bioactivity.endpoint_search.discover_endpoint_candidates", fake_discover)

    result = search_endpoints(_SearchCursor(_saved_endpoint_rows()), "EGFR IC50")

    assert result.candidates[0].display_name == "EGFR reference IC50"
