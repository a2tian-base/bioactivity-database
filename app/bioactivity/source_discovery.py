from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any


class SourceDiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class TargetCandidate:
    source_name: str
    source_target_id: str
    preferred_name: str
    gene_symbol: str | None
    organism: str | None
    target_type: str | None
    identifiers: dict[str, str]
    raw: dict[str, Any]


@dataclass(frozen=True)
class SourceAvailability:
    source_name: str
    source_target_id: str
    measurement_type: str
    approximate_count: int | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class EndpointCandidate:
    candidate_key: str
    display_name: str
    template_key: str
    spec: dict[str, Any]
    source_configs: dict[str, dict[str, Any]]
    source_availability: tuple[SourceAvailability, ...]
    warnings: tuple[str, ...]
    score: float


@dataclass(frozen=True)
class SourceDiscoveryResult:
    candidates: tuple[EndpointCandidate, ...]
    warnings: tuple[str, ...]
    failed_sources: tuple[str, ...]


_PUBCHEM_ACTIVITY_NAME_REGEX_BY_MEASUREMENT = {
    "IC50": r"(?i)\bIC50\b",
    "Ki": r"(?i)\bKi\b",
    "EC50": r"(?i)\bEC50\b",
}
_PUBCHEM_MISSING_NCBI_GENE_ID_WARNING = "PubChem config unavailable: missing NCBI Gene ID."


def _clean_source_name(value: object) -> str:
    return str(value or "").strip().lower()


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    return str(value or "").strip()


def _as_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _with_candidate_warning(candidate: EndpointCandidate, warning: str) -> EndpointCandidate:
    if warning in candidate.warnings:
        return candidate
    return replace(candidate, warnings=(*candidate.warnings, warning))


def maybe_add_pubchem_source_config(candidate: EndpointCandidate) -> EndpointCandidate:
    spec = _as_mapping(candidate.spec)
    target = _as_mapping(spec.get("target"))
    identifiers = _as_mapping(target.get("identifiers"))
    measurement = _as_mapping(spec.get("measurement"))

    ncbi_gene_id = _clean_text(identifiers.get("ncbi_gene_id"))
    if not ncbi_gene_id:
        return _with_candidate_warning(candidate, _PUBCHEM_MISSING_NCBI_GENE_ID_WARNING)

    gene_symbol = _clean_text(target.get("gene_symbol"))
    if not gene_symbol:
        return _with_candidate_warning(candidate, _PUBCHEM_MISSING_NCBI_GENE_ID_WARNING)

    measurement_type = _clean_text(measurement.get("type"))
    activity_name_regex = _PUBCHEM_ACTIVITY_NAME_REGEX_BY_MEASUREMENT.get(measurement_type)
    if activity_name_regex is None:
        return candidate

    return replace(
        candidate,
        source_configs={
            **candidate.source_configs,
            "pubchem": {
                "target_gene_symbol": gene_symbol,
                "target_gene_id": ncbi_gene_id,
                "activity_name_regex": activity_name_regex,
            },
        },
    )


def discover_endpoint_candidates(
    query: Any,
    *,
    sources: Sequence[str] = ("chembl",),
    limit: int = 10,
) -> SourceDiscoveryResult:
    if limit <= 0:
        return SourceDiscoveryResult(candidates=(), warnings=(), failed_sources=())

    clean_sources = tuple(source for source in (_clean_source_name(source) for source in sources) if source)
    candidates: list[EndpointCandidate] = []
    warnings: list[str] = []
    failed_sources: list[str] = []
    pubchem_requested = "pubchem" in clean_sources
    discovery_sources = tuple(source_name for source_name in clean_sources if source_name != "pubchem")

    for source_name in discovery_sources:
        try:
            if source_name == "chembl":
                from .sources.chembl_discovery import build_chembl_endpoint_candidates, search_chembl_targets

                targets = search_chembl_targets(query, limit=limit)
                candidates.extend(build_chembl_endpoint_candidates(query, targets, limit=limit))
            else:
                failed_sources.append(source_name)
                warnings.append(f"Endpoint discovery source '{source_name}' is not supported.")
        except Exception as exc:
            failed_sources.append(source_name)
            warnings.append(f"{source_name} endpoint discovery failed: {exc}")

    if pubchem_requested:
        candidates = [maybe_add_pubchem_source_config(candidate) for candidate in candidates]

    candidates.sort(key=lambda candidate: (-candidate.score, candidate.display_name.lower(), candidate.candidate_key))
    return SourceDiscoveryResult(
        candidates=tuple(candidates[:limit]),
        warnings=tuple(warnings),
        failed_sources=tuple(failed_sources),
    )
