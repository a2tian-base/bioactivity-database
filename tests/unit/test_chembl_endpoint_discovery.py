from __future__ import annotations

from dataclasses import asdict
import json
import urllib.request

from bioactivity.endpoint_search import parse_endpoint_query
from bioactivity.endpoints import EndpointConfig
from bioactivity.sources.chembl_discovery import (
    build_chembl_endpoint_candidates,
    fetch_chembl_activity_availability,
    search_chembl_targets,
    target_candidate_from_chembl_payload,
)
from herg.config import HttpConfig
from herg.sources.chembl import ChemblAdapter


def _chembl_target_payload(
    *,
    target_chembl_id: str,
    pref_name: str,
    gene_symbols: tuple[str, ...],
    organism: str = "Homo sapiens",
    target_type: str = "SINGLE PROTEIN",
    accession: str = "P00533",
) -> dict:
    return {
        "target_chembl_id": target_chembl_id,
        "pref_name": pref_name,
        "organism": organism,
        "target_type": target_type,
        "target_components": [
            {
                "accession": accession,
                "component_description": pref_name,
                "relationship": target_type,
                "target_component_synonyms": [
                    {
                        "component_synonym": gene_symbol,
                        "syn_type": "GENE_SYMBOL",
                    }
                    for gene_symbol in gene_symbols
                ],
            }
        ],
        "cross_references": [],
    }


def _egfr_target() -> dict:
    return _chembl_target_payload(
        target_chembl_id="CHEMBL203",
        pref_name="Epidermal growth factor receptor",
        gene_symbols=("EGFR",),
    )


def _fake_chembl_get_json(targets: list[dict], counts: dict[tuple[str, str], int]):
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_get_json(url, params, config, label="ChEMBL"):
        clean_params = dict(params or {})
        calls.append((url, clean_params))
        if url.endswith("/target/search.json"):
            return {
                "page_meta": {"total_count": len(targets)},
                "targets": targets,
            }
        if url.endswith("/activity.json"):
            count_key = (
                str(clean_params["target_chembl_id"]),
                str(clean_params["standard_type"]),
            )
            return {
                "page_meta": {"total_count": counts.get(count_key, 0)},
                "activities": [],
            }
        raise AssertionError(f"Unexpected ChEMBL URL: {url}")

    return fake_get_json, calls


def test_chembl_target_response_converts_to_target_candidate(monkeypatch):
    fake_get_json, calls = _fake_chembl_get_json([_egfr_target()], {})
    monkeypatch.setattr("bioactivity.sources.chembl_discovery.get_json", fake_get_json)

    targets = search_chembl_targets(parse_endpoint_query("EGFR IC50"), limit=5)

    assert len(targets) == 1
    assert targets[0].source_name == "chembl"
    assert targets[0].source_target_id == "CHEMBL203"
    assert targets[0].preferred_name == "Epidermal growth factor receptor"
    assert targets[0].gene_symbol == "EGFR"
    assert targets[0].organism == "Homo sapiens"
    assert targets[0].target_type == "SINGLE PROTEIN"
    assert targets[0].identifiers == {
        "chembl_target_id": "CHEMBL203",
        "uniprot_accession": "P00533",
    }
    assert calls[0][1]["q"] == "EGFR"


def test_chembl_activity_page_meta_total_count_converts_to_source_availability(monkeypatch):
    fake_get_json, calls = _fake_chembl_get_json([], {("CHEMBL203", "IC50"): 24585})
    monkeypatch.setattr("bioactivity.sources.chembl_discovery.get_json", fake_get_json)

    availability = fetch_chembl_activity_availability(
        target_candidate_from_chembl_payload(_egfr_target()),
        "IC50",
    )

    assert availability.source_name == "chembl"
    assert availability.source_target_id == "CHEMBL203"
    assert availability.measurement_type == "IC50"
    assert availability.approximate_count == 24585
    assert availability.warnings == ()
    assert calls[0][1]["standard_relation__in"] == "=,<,>"
    assert calls[0][1]["data_validity_comment__isnull"] == "true"


def test_egfr_ic50_produces_json_serializable_endpoint_candidate(monkeypatch):
    fake_get_json, _calls = _fake_chembl_get_json([_egfr_target()], {("CHEMBL203", "IC50"): 24585})
    monkeypatch.setattr("bioactivity.sources.chembl_discovery.get_json", fake_get_json)

    query = parse_endpoint_query("EGFR IC50")
    targets = search_chembl_targets(query)
    candidates = build_chembl_endpoint_candidates(query, targets)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.candidate_key == "chembl_chembl203_ic50"
    assert candidate.display_name == "EGFR IC50"
    assert candidate.template_key == "concentration_potency"
    assert candidate.source_availability[0].approximate_count == 24585

    json.dumps(asdict(candidate))


def test_endpoint_candidate_contains_valid_measurement_spec(monkeypatch):
    fake_get_json, _calls = _fake_chembl_get_json([_egfr_target()], {("CHEMBL203", "IC50"): 1})
    monkeypatch.setattr("bioactivity.sources.chembl_discovery.get_json", fake_get_json)

    query = parse_endpoint_query("EGFR IC50")
    candidate = build_chembl_endpoint_candidates(query, search_chembl_targets(query))[0]

    assert candidate.spec["measurement"] == {
        "type": "IC50",
        "value_kind": "concentration",
        "canonical_unit": "uM",
        "supports_p_value": True,
        "p_value_name": "pIC50",
    }
    assert candidate.spec["normalization"] == {
        "allowed_units": ["pM", "nM", "uM", "mM"],
        "allowed_relations": ["=", "<", ">"],
    }


def test_endpoint_candidate_contains_adapter_compatible_chembl_source_config(monkeypatch):
    fake_get_json, _calls = _fake_chembl_get_json([_egfr_target()], {("CHEMBL203", "IC50"): 1})
    monkeypatch.setattr("bioactivity.sources.chembl_discovery.get_json", fake_get_json)
    query = parse_endpoint_query("EGFR IC50")
    candidate = build_chembl_endpoint_candidates(query, search_chembl_targets(query))[0]

    def fake_status_get_json(url, params, config, label="ChEMBL"):
        if url.endswith("/status.json"):
            return {"chembl_db_version": "mock"}
        raise AssertionError(f"Unexpected ChEMBL adapter URL: {url}")

    monkeypatch.setattr("herg.sources.chembl.get_json", fake_status_get_json)
    endpoint = EndpointConfig(
        endpoint_id=1,
        endpoint_key="egfr_ic50",
        display_name=candidate.display_name,
        spec=candidate.spec,
        source_configs=candidate.source_configs,
        spec_hash="fixture",
        active=True,
    )

    adapter = ChemblAdapter.from_source_config(
        endpoint,
        candidate.source_configs["chembl"],
        http_config=HttpConfig(request_timeout_seconds=1, http_retries=0),
        base_url="https://example.org",
    )

    assert candidate.source_configs["chembl"] == {
        "target_chembl_id": "CHEMBL203",
        "standard_type": "IC50",
        "standard_relation__in": ["=", "<", ">"],
        "data_validity_comment__isnull": True,
    }
    assert adapter.effective_config["target_chembl_id"] == "CHEMBL203"
    assert adapter.effective_config["standard_type"] == "IC50"
    assert adapter.effective_config["standard_relation__in"] == "=,<,>"


def test_human_exact_gene_symbol_match_ranks_above_fuzzy_nonhuman_target(monkeypatch):
    human_complex = _chembl_target_payload(
        target_chembl_id="CHEMBL4523747",
        pref_name="EGFR/PPP1CA",
        gene_symbols=("EGFR", "PPP1CA"),
        target_type="PROTEIN-PROTEIN INTERACTION",
    )
    mouse_single = _chembl_target_payload(
        target_chembl_id="CHEMBL3608",
        pref_name="Epidermal growth factor receptor",
        gene_symbols=("Egfr",),
        organism="Mus musculus",
    )
    fake_get_json, _calls = _fake_chembl_get_json(
        [human_complex, mouse_single, _egfr_target()],
        {
            ("CHEMBL4523747", "IC50"): 100,
            ("CHEMBL3608", "IC50"): 100,
            ("CHEMBL203", "IC50"): 100,
        },
    )
    monkeypatch.setattr("bioactivity.sources.chembl_discovery.get_json", fake_get_json)

    query = parse_endpoint_query("EGFR human IC50")
    candidates = build_chembl_endpoint_candidates(query, search_chembl_targets(query), limit=3)

    assert [candidate.source_configs["chembl"]["target_chembl_id"] for candidate in candidates] == [
        "CHEMBL203",
        "CHEMBL3608",
        "CHEMBL4523747",
    ]


def test_zero_count_candidate_is_warned_and_ranked_below_nonzero_candidate(monkeypatch):
    alternate_egfr_target = _chembl_target_payload(
        target_chembl_id="CHEMBL999999",
        pref_name="EGFR reference target",
        gene_symbols=("EGFR",),
        accession="P00534",
    )
    fake_get_json, _calls = _fake_chembl_get_json(
        [_egfr_target(), alternate_egfr_target],
        {
            ("CHEMBL203", "IC50"): 0,
            ("CHEMBL999999", "IC50"): 25,
        },
    )
    monkeypatch.setattr("bioactivity.sources.chembl_discovery.get_json", fake_get_json)

    query = parse_endpoint_query("EGFR")
    candidates = build_chembl_endpoint_candidates(query, search_chembl_targets(query), limit=4)

    assert candidates[0].source_availability[0].measurement_type == "IC50"
    assert candidates[0].source_availability[0].approximate_count == 25
    zero_count_candidate = next(
        candidate
        for candidate in candidates
        if candidate.source_configs["chembl"]["target_chembl_id"] == "CHEMBL203"
    )
    assert zero_count_candidate.source_availability[0].approximate_count == 0
    assert zero_count_candidate.warnings == ("No ChEMBL IC50 activity records found.",)


def test_broad_query_without_measurement_only_checks_ic50(monkeypatch):
    human_complex = _chembl_target_payload(
        target_chembl_id="CHEMBL4523747",
        pref_name="EGFR/PPP1CA",
        gene_symbols=("EGFR", "PPP1CA"),
        target_type="PROTEIN-PROTEIN INTERACTION",
    )
    mouse_single = _chembl_target_payload(
        target_chembl_id="CHEMBL3608",
        pref_name="Epidermal growth factor receptor",
        gene_symbols=("Egfr",),
        organism="Mus musculus",
    )
    fake_get_json, calls = _fake_chembl_get_json(
        [human_complex, mouse_single, _egfr_target()],
        {
            ("CHEMBL203", "IC50"): 10,
        },
    )
    monkeypatch.setattr("bioactivity.sources.chembl_discovery.get_json", fake_get_json)

    query = parse_endpoint_query("EGFR")
    candidates = build_chembl_endpoint_candidates(query, search_chembl_targets(query), limit=4)

    activity_calls = [params for url, params in calls if url.endswith("/activity.json")]
    assert {params["standard_type"] for params in activity_calls} == {"IC50"}
    assert activity_calls[0]["target_chembl_id"] == "CHEMBL203"
    assert {candidate.source_availability[0].measurement_type for candidate in candidates} == {"IC50"}
    assert candidates[0].source_configs["chembl"]["target_chembl_id"] == "CHEMBL203"


def test_requested_non_ic50_measurement_does_not_produce_candidate_or_activity_check(monkeypatch):
    fake_get_json, calls = _fake_chembl_get_json([_egfr_target()], {("CHEMBL203", "EC50"): 10})
    monkeypatch.setattr("bioactivity.sources.chembl_discovery.get_json", fake_get_json)

    query = parse_endpoint_query("EGFR EC50")
    candidates = build_chembl_endpoint_candidates(query, search_chembl_targets(query), limit=4)

    assert candidates == []
    assert not [params for url, params in calls if url.endswith("/activity.json")]


def test_chembl_discovery_tests_do_not_require_live_http(monkeypatch):
    def fail_urlopen(*args, **kwargs):
        raise AssertionError("Live HTTP should not be used in endpoint discovery tests.")

    fake_get_json, _calls = _fake_chembl_get_json([_egfr_target()], {("CHEMBL203", "IC50"): 1})
    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    monkeypatch.setattr("bioactivity.sources.chembl_discovery.get_json", fake_get_json)

    query = parse_endpoint_query("EGFR IC50")
    candidates = build_chembl_endpoint_candidates(query, search_chembl_targets(query))

    assert candidates[0].display_name == "EGFR IC50"
