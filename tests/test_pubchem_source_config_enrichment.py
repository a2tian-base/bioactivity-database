from __future__ import annotations

import urllib.request

from bioactivity.endpoint_search import parse_endpoint_query
from bioactivity.source_discovery import (
    EndpointCandidate,
    SourceAvailability,
    SourceDiscoveryResult,
    discover_endpoint_candidates,
    maybe_add_pubchem_source_config,
)


def _candidate(
    *,
    measurement_type: str = "IC50",
    ncbi_gene_id: str | None = "3757",
    gene_symbol: str | None = "KCNH2",
) -> EndpointCandidate:
    identifiers = {"chembl_target_id": "CHEMBL240"}
    if ncbi_gene_id is not None:
        identifiers["ncbi_gene_id"] = ncbi_gene_id

    return EndpointCandidate(
        candidate_key=f"chembl_chembl240_{measurement_type.lower()}",
        display_name=f"{gene_symbol or 'hERG'} {measurement_type}",
        template_key="concentration_potency",
        spec={
            "target": {
                "preferred_name": "hERG",
                "gene_symbol": gene_symbol,
                "organism": "Homo sapiens",
                "identifiers": identifiers,
            },
            "measurement": {
                "type": measurement_type,
                "value_kind": "concentration",
            },
        },
        source_configs={
            "chembl": {
                "target_chembl_id": "CHEMBL240",
                "standard_type": measurement_type,
                "standard_relation__in": ["=", "<", ">"],
                "data_validity_comment__isnull": True,
            }
        },
        source_availability=(
            SourceAvailability(
                source_name="chembl",
                source_target_id="CHEMBL240",
                measurement_type=measurement_type,
                approximate_count=25,
            ),
        ),
        warnings=(),
        score=100.0,
    )


def test_candidate_with_ncbi_gene_id_gets_pubchem_config():
    enriched = maybe_add_pubchem_source_config(_candidate())

    assert enriched.source_configs["pubchem"] == {
        "target_gene_symbol": "KCNH2",
        "target_gene_id": "3757",
        "activity_name_regex": r"(?i)\bIC50\b",
    }


def test_candidate_without_ncbi_gene_id_gets_warning_and_no_pubchem_config():
    enriched = maybe_add_pubchem_source_config(_candidate(ncbi_gene_id=None))

    assert "pubchem" not in enriched.source_configs
    assert enriched.warnings == ("PubChem config unavailable: missing NCBI Gene ID.",)


def test_ic50_regex_is_adapter_configured_shape():
    enriched = maybe_add_pubchem_source_config(_candidate(measurement_type="IC50"))

    assert enriched.source_configs["pubchem"]["activity_name_regex"] == r"(?i)\bIC50\b"


def test_ki_regex_is_adapter_configured_shape():
    enriched = maybe_add_pubchem_source_config(
        _candidate(measurement_type="Ki", ncbi_gene_id="1813", gene_symbol="DRD2")
    )

    assert enriched.source_configs["pubchem"] == {
        "target_gene_symbol": "DRD2",
        "target_gene_id": "1813",
        "activity_name_regex": r"(?i)\bKi\b",
    }


def test_existing_chembl_config_is_preserved():
    candidate = _candidate()
    chembl_config = dict(candidate.source_configs["chembl"])

    enriched = maybe_add_pubchem_source_config(candidate)

    assert enriched.source_configs["chembl"] == chembl_config


def test_pubchem_enrichment_in_discovery_path_does_not_call_http(monkeypatch):
    def fail_urlopen(*args, **kwargs):
        raise AssertionError("Live HTTP should not be used for PubChem config enrichment.")

    def fake_search_chembl_targets(query, *, limit):
        assert query.target_query == "KCNH2"
        return []

    def fake_build_chembl_endpoint_candidates(query, targets, *, limit):
        return [_candidate()]

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    monkeypatch.setattr("bioactivity.sources.chembl_discovery.search_chembl_targets", fake_search_chembl_targets)
    monkeypatch.setattr(
        "bioactivity.sources.chembl_discovery.build_chembl_endpoint_candidates",
        fake_build_chembl_endpoint_candidates,
    )

    result = discover_endpoint_candidates(
        parse_endpoint_query("KCNH2 IC50"),
        sources=("chembl", "pubchem"),
    )

    assert isinstance(result, SourceDiscoveryResult)
    assert result.candidates[0].source_configs["pubchem"]["target_gene_id"] == "3757"
    assert result.warnings == ()
    assert result.failed_sources == ()
