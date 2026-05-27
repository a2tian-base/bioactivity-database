from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
