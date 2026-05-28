from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import re
from typing import Any

import psycopg


_MEASUREMENT_TERMS = (
    "percent inhibition",
    "activity outcome",
    "IC50",
    "EC50",
    "AC50",
    "Ki",
    "Kd",
    "Potency",
    "inhibition",
    "activation",
    "outcome",
)
_MEASUREMENT_TERM_MAP = {
    "percent inhibition": "percent inhibition",
    "activity outcome": "activity outcome",
    "ic50": "IC50",
    "ec50": "EC50",
    "ac50": "AC50",
    "ki": "Ki",
    "kd": "Kd",
    "potency": "Potency",
    "inhibition": "inhibition",
    "activation": "activation",
    "outcome": "outcome",
}


def _measurement_term_pattern(term: str) -> str:
    term_pattern = r"\s+".join(re.escape(part) for part in term.split())
    return rf"(?<![0-9A-Za-z]){term_pattern}(?![0-9A-Za-z])"


_MEASUREMENT_PATTERN = re.compile(
    "|".join(_measurement_term_pattern(term) for term in _MEASUREMENT_TERMS),
    re.IGNORECASE,
)
_HUMAN_PATTERN = re.compile(r"(?<![0-9A-Za-z])human(?![0-9A-Za-z])", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedEndpointQuery:
    raw_query: str
    target_query: str
    measurement_types: tuple[str, ...]
    organism: str | None = None


@dataclass(frozen=True)
class SavedEndpointSearchResult:
    endpoint_id: int
    endpoint_key: str
    display_name: str
    target_name: str | None
    gene_symbol: str | None
    organism: str | None
    measurement_type: str | None
    value_kind: str | None
    source_names: tuple[str, ...]
    active: bool
    score: float


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clean_query_text(value: object) -> str:
    text = _clean_text(value).replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"^[^0-9A-Za-z]+|[^0-9A-Za-z]+$", "", text).strip()


def _normalize_measurement_term(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _normalize_search_text(value: object) -> str:
    text = _clean_text(value).lower().replace("\u00b5", "u").replace("\u03bc", "u")
    text = re.sub(r"[^0-9a-z]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _json_search_text(value: object) -> str:
    if isinstance(value, Mapping):
        return _normalize_search_text(json.dumps(dict(value), sort_keys=True))
    return _normalize_search_text(value)


def _as_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return dict(value)


def _optional_text(value: object) -> str | None:
    cleaned = _clean_text(value)
    return cleaned or None


def parse_endpoint_query(query: str) -> ParsedEndpointQuery:
    raw_query = _clean_text(query)
    measurement_types: list[str] = []

    def _strip_measurement(match: re.Match[str]) -> str:
        canonical = _MEASUREMENT_TERM_MAP[_normalize_measurement_term(match.group(0))]
        if canonical not in measurement_types:
            measurement_types.append(canonical)
        return " "

    target_text = _MEASUREMENT_PATTERN.sub(_strip_measurement, raw_query)

    organism = None
    if _HUMAN_PATTERN.search(target_text):
        organism = "Homo sapiens"
        target_text = _HUMAN_PATTERN.sub(" ", target_text)

    return ParsedEndpointQuery(
        raw_query=raw_query,
        target_query=_clean_query_text(target_text),
        measurement_types=tuple(measurement_types),
        organism=organism,
    )


def _fetch_endpoint_rows(
    cur: psycopg.Cursor,
    *,
    include_inactive: bool,
) -> list[Sequence[object]]:
    cur.execute(
        """
        SELECT
            endpoint_id,
            endpoint_key,
            display_name,
            spec,
            source_configs,
            active
        FROM endpoints
        WHERE active = TRUE OR %s
        ORDER BY endpoint_id
        """,
        (include_inactive,),
    )
    return list(cur.fetchall())


def _text_match_score(
    term: str,
    *,
    exact_fields: Sequence[str],
    contains_fields: Sequence[str],
    fallback_fields: Sequence[str],
    exact_score: float,
    contains_score: float,
    fallback_score: float,
) -> tuple[bool, float]:
    if not term:
        return False, 0.0

    if any(field and field == term for field in exact_fields):
        return True, exact_score
    if any(field and term in field for field in contains_fields):
        return True, contains_score
    if any(field and term in field for field in fallback_fields):
        return True, fallback_score
    return False, 0.0


def _score_saved_endpoint(
    parsed: ParsedEndpointQuery,
    *,
    endpoint_key: str,
    display_name: str,
    spec: Mapping[str, Any],
    source_configs: Mapping[str, Any],
) -> float:
    raw_norm = _normalize_search_text(parsed.raw_query)
    target_norm = _normalize_search_text(parsed.target_query)
    organism_norm = _normalize_search_text(parsed.organism)
    measurement_norms = tuple(_normalize_search_text(value) for value in parsed.measurement_types)

    target = _as_dict(spec.get("target"))
    measurement = _as_dict(spec.get("measurement"))

    key_norm = _normalize_search_text(endpoint_key)
    display_norm = _normalize_search_text(display_name)
    target_name_norm = _normalize_search_text(target.get("preferred_name"))
    gene_symbol_norm = _normalize_search_text(target.get("gene_symbol"))
    organism_value_norm = _normalize_search_text(target.get("organism") or _as_dict(spec.get("inclusion_criteria")).get("organism"))
    measurement_type_norm = _normalize_search_text(measurement.get("type"))
    spec_text_norm = _json_search_text(spec)
    source_configs_text_norm = _json_search_text(source_configs)
    has_structured_query = bool(target_norm or measurement_norms or organism_norm)

    score = 0.0
    if raw_norm:
        raw_matched, raw_score = _text_match_score(
            raw_norm,
            exact_fields=(key_norm, display_norm),
            contains_fields=(key_norm, display_norm),
            fallback_fields=(spec_text_norm, source_configs_text_norm),
            exact_score=120.0,
            contains_score=80.0,
            fallback_score=30.0,
        )
        if raw_matched:
            score += raw_score
        elif not has_structured_query:
            return 0.0

    if target_norm:
        target_matched, target_score = _text_match_score(
            target_norm,
            exact_fields=(gene_symbol_norm, target_name_norm, key_norm, display_norm),
            contains_fields=(gene_symbol_norm, target_name_norm, key_norm, display_norm),
            fallback_fields=(spec_text_norm, source_configs_text_norm),
            exact_score=45.0,
            contains_score=28.0,
            fallback_score=10.0,
        )
        if not target_matched:
            return 0.0
        score += target_score

    for measurement_norm in measurement_norms:
        measurement_matched, measurement_score = _text_match_score(
            measurement_norm,
            exact_fields=(measurement_type_norm,),
            contains_fields=(key_norm, display_norm),
            fallback_fields=(spec_text_norm, source_configs_text_norm),
            exact_score=24.0,
            contains_score=16.0,
            fallback_score=8.0,
        )
        if not measurement_matched:
            return 0.0
        score += measurement_score

    if organism_norm:
        organism_matched, organism_score = _text_match_score(
            organism_norm,
            exact_fields=(organism_value_norm,),
            contains_fields=(organism_value_norm,),
            fallback_fields=(spec_text_norm,),
            exact_score=12.0,
            contains_score=8.0,
            fallback_score=4.0,
        )
        if not organism_matched:
            return 0.0
        score += organism_score

    return score


def _result_from_row(row: Sequence[object], *, score: float) -> SavedEndpointSearchResult:
    endpoint_id, endpoint_key, display_name, spec, source_configs, active = row

    spec_dict = _as_dict(spec)
    target = _as_dict(spec_dict.get("target"))
    measurement = _as_dict(spec_dict.get("measurement"))
    source_config_dict = _as_dict(source_configs)

    return SavedEndpointSearchResult(
        endpoint_id=int(endpoint_id),
        endpoint_key=_clean_text(endpoint_key),
        display_name=_clean_text(display_name),
        target_name=_optional_text(target.get("preferred_name")),
        gene_symbol=_optional_text(target.get("gene_symbol")),
        organism=_optional_text(target.get("organism") or _as_dict(spec_dict.get("inclusion_criteria")).get("organism")),
        measurement_type=_optional_text(measurement.get("type")),
        value_kind=_optional_text(measurement.get("value_kind")),
        source_names=tuple(sorted(_clean_text(name) for name in source_config_dict if _clean_text(name))),
        active=bool(active),
        score=float(score),
    )


def _search_from_cursor(
    cur: psycopg.Cursor,
    query: str,
    *,
    limit: int,
    include_inactive: bool,
) -> list[SavedEndpointSearchResult]:
    if limit <= 0:
        return []

    parsed = parse_endpoint_query(query)
    if not parsed.raw_query:
        return []

    results: list[SavedEndpointSearchResult] = []
    for row in _fetch_endpoint_rows(cur, include_inactive=include_inactive):
        if not include_inactive and not bool(row[5]):
            continue

        spec = _as_dict(row[3])
        source_configs = _as_dict(row[4])
        score = _score_saved_endpoint(
            parsed,
            endpoint_key=_clean_text(row[1]),
            display_name=_clean_text(row[2]),
            spec=spec,
            source_configs=source_configs,
        )
        if score <= 0:
            continue
        results.append(_result_from_row(row, score=score))

    results.sort(
        key=lambda result: (
            -result.score,
            _normalize_search_text(result.display_name),
            _normalize_search_text(result.endpoint_key),
            result.endpoint_id,
        )
    )
    return results[:limit]


def search_saved_endpoints(
    conn_or_cur: psycopg.Connection | psycopg.Cursor,
    query: str,
    *,
    limit: int = 20,
    include_inactive: bool = False,
) -> list[SavedEndpointSearchResult]:
    if hasattr(conn_or_cur, "fetchall"):
        return _search_from_cursor(
            conn_or_cur,
            query,
            limit=limit,
            include_inactive=include_inactive,
        )

    with conn_or_cur.cursor() as cur:
        return _search_from_cursor(
            cur,
            query,
            limit=limit,
            include_inactive=include_inactive,
        )
