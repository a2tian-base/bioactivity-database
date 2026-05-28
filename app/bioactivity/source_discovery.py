from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
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


def _clean_source_name(value: object) -> str:
    return str(value or "").strip().lower()


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

    for source_name in clean_sources:
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

    candidates.sort(key=lambda candidate: (-candidate.score, candidate.display_name.lower(), candidate.candidate_key))
    return SourceDiscoveryResult(
        candidates=tuple(candidates[:limit]),
        warnings=tuple(warnings),
        failed_sources=tuple(failed_sources),
    )
