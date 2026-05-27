from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import re
from typing import Any

from bioactivity.endpoint_search import ParsedEndpointQuery
from bioactivity.source_discovery import EndpointCandidate, SourceAvailability, TargetCandidate
from herg.config import HttpConfig
from herg.http import get_json
from herg.normalize import clean_text, dedupe_casefolded
from herg.sources.chembl import CHEMBL_BASE_URL


SOURCE_NAME = "chembl"
COMMON_CONCENTRATION_POTENCY_TYPES = ("IC50", "EC50", "Ki", "Kd")
ALLOWED_CONCENTRATION_UNITS = ("pM", "nM", "uM", "mM")
ALLOWED_RELATIONS = ("=", "<", ">")
TARGET_SEARCH_ONLY_FIELDS = (
    "target_chembl_id,pref_name,organism,target_type,target_components,cross_references"
)


def _normalize_search_text(value: object) -> str:
    if isinstance(value, Mapping):
        value = json.dumps(dict(value), sort_keys=True)
    text = clean_text(value).lower().replace("\u00b5", "u").replace("\u03bc", "u")
    text = re.sub(r"[^0-9a-z]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _slug(value: object) -> str:
    slug = re.sub(r"[^0-9a-z]+", "_", clean_text(value).lower())
    return re.sub(r"_+", "_", slug).strip("_")


def _optional_int(value: object) -> int | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _as_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _as_sequence(value: object) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _gene_symbols_from_target(payload: Mapping[str, Any]) -> list[str]:
    symbols: list[str] = []
    for component in _as_sequence(payload.get("target_components")):
        component_mapping = _as_mapping(component)
        for synonym in _as_sequence(component_mapping.get("target_component_synonyms")):
            synonym_mapping = _as_mapping(synonym)
            if clean_text(synonym_mapping.get("syn_type")).upper() != "GENE_SYMBOL":
                continue
            symbol = clean_text(synonym_mapping.get("component_synonym"))
            if symbol:
                symbols.append(symbol)
    return dedupe_casefolded(symbols)


def _single_accession_from_target(payload: Mapping[str, Any]) -> str | None:
    accessions = dedupe_casefolded(
        clean_text(_as_mapping(component).get("accession"))
        for component in _as_sequence(payload.get("target_components"))
    )
    return accessions[0] if len(accessions) == 1 else None


def target_candidate_from_chembl_payload(payload: Mapping[str, Any]) -> TargetCandidate:
    target_chembl_id = clean_text(payload.get("target_chembl_id"))
    gene_symbols = _gene_symbols_from_target(payload)
    identifiers: dict[str, str] = {}
    if target_chembl_id:
        identifiers["chembl_target_id"] = target_chembl_id

    uniprot_accession = _single_accession_from_target(payload)
    if uniprot_accession:
        identifiers["uniprot_accession"] = uniprot_accession

    return TargetCandidate(
        source_name=SOURCE_NAME,
        source_target_id=target_chembl_id,
        preferred_name=clean_text(payload.get("pref_name")),
        gene_symbol=gene_symbols[0] if len(gene_symbols) == 1 else None,
        organism=clean_text(payload.get("organism")) or None,
        target_type=clean_text(payload.get("target_type")) or None,
        identifiers=identifiers,
        raw=dict(payload),
    )


def search_chembl_targets(
    query: ParsedEndpointQuery,
    *,
    limit: int = 10,
    http_config: HttpConfig | None = None,
) -> list[TargetCandidate]:
    if limit <= 0 or not clean_text(query.target_query):
        return []

    payload = get_json(
        f"{CHEMBL_BASE_URL}/target/search.json",
        {
            "q": query.target_query,
            "limit": limit,
            "only": TARGET_SEARCH_ONLY_FIELDS,
        },
        http_config or HttpConfig(),
        label="ChEMBL",
    )
    targets = _as_sequence(payload.get("targets"))
    return [target_candidate_from_chembl_payload(_as_mapping(target)) for target in targets[:limit]]


def fetch_chembl_activity_availability(
    target: TargetCandidate,
    measurement_type: str,
    *,
    http_config: HttpConfig | None = None,
) -> SourceAvailability:
    clean_measurement_type = clean_text(measurement_type)
    if not target.source_target_id:
        return SourceAvailability(
            source_name=SOURCE_NAME,
            source_target_id=target.source_target_id,
            measurement_type=clean_measurement_type,
            approximate_count=None,
            warnings=("ChEMBL target ID is missing; activity availability was not checked.",),
        )

    payload = get_json(
        f"{CHEMBL_BASE_URL}/activity.json",
        {
            "target_chembl_id": target.source_target_id,
            "standard_type": clean_measurement_type,
            "standard_relation__in": ",".join(ALLOWED_RELATIONS),
            "data_validity_comment__isnull": "true",
            "only": "activity_id",
            "limit": 1,
        },
        http_config or HttpConfig(),
        label="ChEMBL",
    )
    page_meta = _as_mapping(payload.get("page_meta"))
    approximate_count = _optional_int(page_meta.get("total_count"))
    warnings = ()
    if approximate_count is None:
        warnings = ("ChEMBL activity availability count was not returned.",)
    elif approximate_count == 0:
        warnings = (f"No ChEMBL {clean_measurement_type} activity records found.",)

    return SourceAvailability(
        source_name=SOURCE_NAME,
        source_target_id=target.source_target_id,
        measurement_type=clean_measurement_type,
        approximate_count=approximate_count,
        warnings=warnings,
    )


def _measurement_types_for_query(query: ParsedEndpointQuery) -> tuple[str, ...]:
    if not query.measurement_types:
        return COMMON_CONCENTRATION_POTENCY_TYPES
    requested = [clean_text(value) for value in query.measurement_types]
    return tuple(
        measurement_type
        for measurement_type in requested
        if measurement_type in COMMON_CONCENTRATION_POTENCY_TYPES
    )


def _target_warnings(target: TargetCandidate) -> tuple[str, ...]:
    warnings: list[str] = []
    if not target.source_target_id:
        warnings.append("ChEMBL target ID is missing.")
    if not target.preferred_name:
        warnings.append("ChEMBL target preferred name is missing.")
    if not target.gene_symbol:
        warnings.append("ChEMBL target gene symbol is unavailable or ambiguous.")
    if not target.organism:
        warnings.append("ChEMBL target organism is missing.")
    if clean_text(target.target_type).upper() != "SINGLE PROTEIN":
        warnings.append("ChEMBL target is not a SINGLE PROTEIN target.")
    return tuple(warnings)


def _build_endpoint_spec(target: TargetCandidate, measurement_type: str) -> dict[str, Any]:
    return {
        "target": {
            "preferred_name": target.preferred_name,
            "gene_symbol": target.gene_symbol,
            "organism": target.organism,
            "identifiers": {
                "chembl_target_id": target.source_target_id,
            },
        },
        "measurement": {
            "type": measurement_type,
            "value_kind": "concentration",
            "canonical_unit": "uM",
            "supports_p_value": True,
            "p_value_name": f"p{measurement_type}",
        },
        "normalization": {
            "allowed_units": list(ALLOWED_CONCENTRATION_UNITS),
            "allowed_relations": list(ALLOWED_RELATIONS),
        },
        "inclusion_criteria": {
            "direct_target_only": True,
        },
    }


def _build_source_configs(target: TargetCandidate, measurement_type: str) -> dict[str, dict[str, Any]]:
    return {
        SOURCE_NAME: {
            "target_chembl_id": target.source_target_id,
            "standard_type": measurement_type,
            "standard_relation__in": list(ALLOWED_RELATIONS),
            "data_validity_comment__isnull": True,
        }
    }


def _display_name(target: TargetCandidate, measurement_type: str) -> str:
    target_name = target.gene_symbol or target.preferred_name or target.source_target_id or "ChEMBL target"
    return f"{target_name} {measurement_type}"


def _candidate_key(target: TargetCandidate, measurement_type: str) -> str:
    source_target_id = _slug(target.source_target_id) or "unknown_target"
    measurement_slug = _slug(measurement_type) or "measurement"
    return f"{SOURCE_NAME}_{source_target_id}_{measurement_slug}"


def _score_candidate(
    query: ParsedEndpointQuery,
    target: TargetCandidate,
    availability: SourceAvailability,
) -> float:
    score = _score_target_metadata(query, target)

    if availability.approximate_count is None:
        score -= 5.0
    elif availability.approximate_count == 0:
        score -= 30.0
    else:
        score += 20.0
        score += min(float(availability.approximate_count), 10000.0) / 1000.0

    return score


def _score_target_metadata(query: ParsedEndpointQuery, target: TargetCandidate) -> float:
    target_query_norm = _normalize_search_text(query.target_query)
    gene_norm = _normalize_search_text(target.gene_symbol)
    preferred_name_norm = _normalize_search_text(target.preferred_name)
    organism_norm = _normalize_search_text(target.organism)
    requested_organism_norm = _normalize_search_text(query.organism)
    target_type_norm = _normalize_search_text(target.target_type)

    score = 0.0
    if not target.source_target_id:
        score -= 50.0

    if target_query_norm:
        if gene_norm and gene_norm == target_query_norm:
            score += 80.0
        elif preferred_name_norm and preferred_name_norm == target_query_norm:
            score += 70.0
        elif preferred_name_norm and target_query_norm in preferred_name_norm:
            score += 35.0
        elif target_query_norm in _normalize_search_text(target.raw):
            score += 10.0

    if requested_organism_norm:
        if organism_norm == requested_organism_norm:
            score += 25.0
        else:
            score -= 40.0
    elif organism_norm == "homo sapiens":
        score += 12.0
    elif organism_norm:
        score -= 5.0

    if target_type_norm == "single protein":
        score += 24.0
    elif target_type_norm:
        score -= 6.0

    if not target.preferred_name:
        score -= 12.0
    if not target.gene_symbol:
        score -= 8.0

    return score


def _build_candidate(
    query: ParsedEndpointQuery,
    target: TargetCandidate,
    measurement_type: str,
    availability: SourceAvailability,
) -> EndpointCandidate:
    warnings = _target_warnings(target) + availability.warnings
    return EndpointCandidate(
        candidate_key=_candidate_key(target, measurement_type),
        display_name=_display_name(target, measurement_type),
        template_key="concentration_potency",
        spec=_build_endpoint_spec(target, measurement_type),
        source_configs=_build_source_configs(target, measurement_type),
        source_availability=(availability,),
        warnings=warnings,
        score=_score_candidate(query, target, availability),
    )


def build_chembl_endpoint_candidates(
    query: ParsedEndpointQuery,
    targets: Sequence[TargetCandidate],
    *,
    limit: int = 10,
    http_config: HttpConfig | None = None,
) -> list[EndpointCandidate]:
    if limit <= 0:
        return []

    measurement_types = _measurement_types_for_query(query)
    if not measurement_types:
        return []

    candidate_limit = max(1, limit)
    max_availability_checks = (
        candidate_limit * max(1, len(query.measurement_types))
        if query.measurement_types
        else candidate_limit
    )
    ranked_targets = sorted(
        targets,
        key=lambda target: (
            -_score_target_metadata(query, target),
            _normalize_search_text(target.preferred_name),
            target.source_target_id,
        ),
    )
    candidates: list[EndpointCandidate] = []
    checks = 0

    for target in ranked_targets:
        if checks >= max_availability_checks:
            break
        for measurement_type in measurement_types:
            if checks >= max_availability_checks:
                break
            availability = fetch_chembl_activity_availability(
                target,
                measurement_type,
                http_config=http_config,
            )
            checks += 1
            candidates.append(_build_candidate(query, target, measurement_type, availability))

    candidates.sort(
        key=lambda candidate: (
            -candidate.score,
            _normalize_search_text(candidate.display_name),
            candidate.candidate_key,
        )
    )
    return candidates[:candidate_limit]
