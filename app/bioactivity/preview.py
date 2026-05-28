from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import psycopg

from herg.config import DbConfig, HttpConfig
from herg.db import get_conn
from herg.models import StagedRecord
from herg.normalize import clean_text
from herg.pipeline import _validate_staged_record
from herg.sources.chembl import ChemblAdapter, measurement_input_from_chembl_record
from herg.sources.pubchem import PubChemAdapter, measurement_input_from_pubchem_record

from .endpoints import EndpointConfig, EndpointConfigError, get_source_config, load_endpoint
from .models import MeasurementInput, measurement_from_ic50


class PreviewError(ValueError):
    pass


class UnsupportedSourceError(PreviewError):
    pass


class PreviewAdapter(Protocol):
    source_name: str

    def iter_raw_rows(self):
        ...

    def enrich_batch(self, rows: list[dict]) -> list[dict]:
        ...

    def map_row(self, row: dict) -> StagedRecord:
        ...


AdapterFactory = Callable[[EndpointConfig, dict[str, Any], HttpConfig, int], PreviewAdapter]
MeasurementFactory = Callable[[StagedRecord], MeasurementInput]


@dataclass(frozen=True)
class PreviewExample:
    external_key: str
    source_record_key: str = ""
    measurement: dict[str, Any] = field(default_factory=dict)
    raw_summary: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class PreviewResult:
    endpoint_key: str
    source_name: str
    query_config: dict[str, Any]
    raw_rows_examined: int
    accepted_count: int
    skipped_count: int
    error_count: int
    accepted_examples: list[PreviewExample] = field(default_factory=list)
    skipped_examples: list[PreviewExample] = field(default_factory=list)
    error_examples: list[PreviewExample] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _chembl_factory(
    endpoint: EndpointConfig,
    source_config: dict[str, Any],
    http_config: HttpConfig,
    limit: int,
) -> PreviewAdapter:
    return ChemblAdapter.from_source_config(
        endpoint,
        source_config,
        http_config=http_config,
        activity_page_size=limit,
    )


def _pubchem_factory(
    endpoint: EndpointConfig,
    source_config: dict[str, Any],
    http_config: HttpConfig,
    limit: int,
) -> PreviewAdapter:
    return PubChemAdapter.from_source_config(endpoint, source_config, http_config=http_config)


DEFAULT_ADAPTER_FACTORIES: dict[str, AdapterFactory] = {
    "chembl": _chembl_factory,
    "pubchem": _pubchem_factory,
}

DEFAULT_MEASUREMENT_FACTORIES: dict[str, MeasurementFactory] = {
    "chembl": measurement_input_from_chembl_record,
    "pubchem": measurement_input_from_pubchem_record,
}


def _normalize_source_name(source_name: str) -> str:
    clean_source_name = clean_text(source_name).lower()
    if not clean_source_name:
        raise PreviewError("source is required.")
    return clean_source_name


def _raw_summary(row: object, *, max_keys: int = 8) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        return {"value": clean_text(row)[:160]}
    summary: dict[str, Any] = {}
    for key, value in list(row.items())[:max_keys]:
        if isinstance(value, Mapping):
            summary[str(key)] = f"object({len(value)})"
        elif isinstance(value, list):
            summary[str(key)] = f"list({len(value)})"
        else:
            summary[str(key)] = clean_text(value)[:160]
    return summary


def _measurement_summary(measurement: MeasurementInput) -> dict[str, Any]:
    return {
        "result_key": measurement.result_key,
        "measurement_type": measurement.measurement_type,
        "value_kind": measurement.value_kind,
        "original_value": str(measurement.original_value) if measurement.original_value is not None else None,
        "original_unit": measurement.original_unit,
        "original_relation": measurement.original_relation,
        "standard_value": str(measurement.standard_value) if measurement.standard_value is not None else None,
        "standard_unit": measurement.standard_unit,
        "standard_relation": measurement.standard_relation,
        "p_value": str(measurement.p_value) if measurement.p_value is not None else None,
        "p_value_relation": measurement.p_value_relation,
        "assay_context": measurement.assay_context,
        "quality_flags": measurement.quality_flags,
    }


def _fallback_measurement_from_staged_record(record: StagedRecord, source_name: str) -> MeasurementInput:
    return measurement_from_ic50(
        result_key=clean_text(record.external_key) or clean_text(record.source_record.source_record_key),
        ic50_value=record.measurement.ic50_value,
        ic50_unit=record.measurement.ic50_unit,
        qualifier=record.measurement.qualifier,
        quality_flags={"source": source_name},
    )


def _measurement_from_record(
    record: StagedRecord,
    source_name: str,
    measurement_factories: Mapping[str, MeasurementFactory],
) -> MeasurementInput:
    factory = measurement_factories.get(source_name)
    if factory is not None:
        return factory(record)
    return _fallback_measurement_from_staged_record(record, source_name)


def _iter_limited_rows(adapter: PreviewAdapter, limit: int) -> list[dict]:
    rows: list[dict] = []
    for row in adapter.iter_raw_rows():
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _build_preview_adapter(
    factory: AdapterFactory,
    endpoint: EndpointConfig,
    source_name: str,
    source_config: dict[str, Any],
    http_config: HttpConfig,
    limit: int,
) -> PreviewAdapter:
    try:
        return factory(endpoint, source_config, http_config, limit)
    except PreviewError:
        raise
    except EndpointConfigError:
        raise
    except ValueError as exc:
        raise PreviewError(f"Invalid source config for '{source_name}': {exc}") from exc


def preview_endpoint_source(
    conn_or_cur: psycopg.Connection | psycopg.Cursor,
    *,
    endpoint_key: str,
    source_name: str,
    limit: int = 20,
    http_config: HttpConfig | None = None,
    adapter_factories: Mapping[str, AdapterFactory] | None = None,
    measurement_factories: Mapping[str, MeasurementFactory] | None = None,
) -> PreviewResult:
    if limit <= 0:
        raise PreviewError("limit must be > 0.")

    normalized_source_name = _normalize_source_name(source_name)
    factories = dict(adapter_factories or DEFAULT_ADAPTER_FACTORIES)
    if normalized_source_name not in factories:
        supported = ", ".join(sorted(factories))
        raise UnsupportedSourceError(f"Unsupported source '{normalized_source_name}'. Supported sources: {supported}.")

    endpoint = load_endpoint(conn_or_cur, endpoint_key)
    source_config = get_source_config(endpoint, normalized_source_name)
    adapter = _build_preview_adapter(
        factories[normalized_source_name],
        endpoint,
        normalized_source_name,
        source_config,
        http_config or HttpConfig(),
        limit,
    )
    measurement_factory_map = dict(measurement_factories or DEFAULT_MEASUREMENT_FACTORIES)

    raw_rows = _iter_limited_rows(adapter, limit)
    warnings: list[str] = []
    try:
        rows = adapter.enrich_batch(raw_rows)
    except Exception as exc:
        raise PreviewError(f"Failed to enrich preview rows for source '{normalized_source_name}': {exc}") from exc
    if len(rows) != len(raw_rows):
        warnings.append(f"Adapter returned {len(rows)} enriched rows for {len(raw_rows)} raw rows.")

    accepted_examples: list[PreviewExample] = []
    skipped_examples: list[PreviewExample] = []
    error_examples: list[PreviewExample] = []
    accepted_count = 0
    skipped_count = 0
    error_count = 0

    for row in rows:
        external_key = clean_text(row.get("external_key", "")) if isinstance(row, Mapping) else ""
        try:
            staged = adapter.map_row(row)
            _validate_staged_record(staged)
            measurement = _measurement_from_record(staged, normalized_source_name, measurement_factory_map)
            accepted_count += 1
            accepted_examples.append(
                PreviewExample(
                    external_key=clean_text(staged.external_key),
                    source_record_key=clean_text(staged.source_record.source_record_key),
                    measurement=_measurement_summary(measurement),
                    raw_summary=_raw_summary(row),
                )
            )
        except ValueError as exc:
            skipped_count += 1
            skipped_examples.append(
                PreviewExample(
                    external_key=external_key,
                    raw_summary=_raw_summary(row),
                    reason=str(exc),
                )
            )
        except Exception as exc:
            error_count += 1
            error_examples.append(
                PreviewExample(
                    external_key=external_key,
                    raw_summary=_raw_summary(row),
                    reason=str(exc),
                )
            )

    return PreviewResult(
        endpoint_key=endpoint.endpoint_key,
        source_name=normalized_source_name,
        query_config=dict(getattr(adapter, "effective_config", source_config)),
        raw_rows_examined=len(raw_rows),
        accepted_count=accepted_count,
        skipped_count=skipped_count,
        error_count=error_count,
        accepted_examples=accepted_examples,
        skipped_examples=skipped_examples,
        error_examples=error_examples,
        warnings=warnings,
    )


def _json_line(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def format_preview_result(result: PreviewResult) -> str:
    lines = [
        "Endpoint",
        f"  {result.endpoint_key}",
        "",
        "Source",
        f"  {result.source_name}",
        "",
        "Query config",
        f"  {_json_line(result.query_config)}",
        "",
        "Summary",
        f"  raw rows examined: {result.raw_rows_examined}",
        f"  accepted: {result.accepted_count}",
        f"  skipped: {result.skipped_count}",
        f"  errors: {result.error_count}",
        "",
        "Accepted examples",
    ]
    if result.accepted_examples:
        for example in result.accepted_examples:
            lines.append(f"  - {example.external_key or example.source_record_key}: {_json_line(example.measurement)}")
    else:
        lines.append("  none")

    lines.extend(["", "Skipped examples"])
    if result.skipped_examples:
        for example in result.skipped_examples:
            label = example.external_key or _json_line(example.raw_summary)
            lines.append(f"  - {label}: {example.reason}")
    else:
        lines.append("  none")

    if result.error_examples:
        lines.extend(["", "Error examples"])
        for example in result.error_examples:
            label = example.external_key or _json_line(example.raw_summary)
            lines.append(f"  - {label}: {example.reason}")

    lines.extend(["", "Warnings"])
    if result.warnings:
        for warning in result.warnings:
            lines.append(f"  - {warning}")
    else:
        lines.append("  none")

    return "\n".join(lines)


def _db_config_from_args(args: argparse.Namespace) -> DbConfig:
    config = DbConfig.from_env()
    return DbConfig(
        host=args.db_host or config.host,
        port=args.db_port or config.port,
        dbname=args.db_name or config.dbname,
        user=args.db_user or config.user,
        password=args.db_password or config.password,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview source rows for a saved bioactivity endpoint.")
    parser.add_argument("--endpoint", required=True, dest="endpoint_key")
    parser.add_argument("--source", required=True, dest="source_name")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--request-timeout-seconds", type=int, default=45)
    parser.add_argument("--http-retries", type=int, default=4)
    parser.add_argument("--db-host", default=None)
    parser.add_argument("--db-port", type=int, default=None)
    parser.add_argument("--db-name", default=None)
    parser.add_argument("--db-user", default=None)
    parser.add_argument("--db-password", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    http_config = HttpConfig(
        request_timeout_seconds=args.request_timeout_seconds,
        http_retries=args.http_retries,
    )
    db_config = _db_config_from_args(args)
    try:
        with get_conn(db_config=db_config) as conn:
            result = preview_endpoint_source(
                conn,
                endpoint_key=args.endpoint_key,
                source_name=args.source_name,
                limit=args.limit,
                http_config=http_config,
            )
    except (EndpointConfigError, PreviewError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(format_preview_result(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
