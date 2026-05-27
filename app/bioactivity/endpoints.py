from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import psycopg

from .models import ALLOWED_VALUE_KINDS


class EndpointConfigError(ValueError):
    pass


class EndpointNotFoundError(EndpointConfigError):
    pass


class InactiveEndpointError(EndpointConfigError):
    pass


class MissingSourceConfigError(EndpointConfigError):
    pass


@dataclass(frozen=True)
class EndpointConfig:
    endpoint_id: int
    endpoint_key: str
    display_name: str
    spec: dict[str, Any]
    source_configs: dict[str, dict[str, Any]]
    spec_hash: str
    active: bool

    def source_config(self, source_name: str) -> dict[str, Any]:
        return get_source_config(self, source_name)


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _validate_mapping(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EndpointConfigError(f"Endpoint {field_name} must be an object.")
    return dict(value)


def _validate_source_configs(value: object) -> dict[str, dict[str, Any]]:
    source_configs = _validate_mapping(value, "source_configs")
    validated: dict[str, dict[str, Any]] = {}
    for source_name, source_config in source_configs.items():
        clean_source_name = _clean_text(source_name)
        if not clean_source_name:
            raise EndpointConfigError("Endpoint source_configs keys must be non-empty.")
        if not isinstance(source_config, Mapping):
            raise EndpointConfigError(f"Endpoint source_configs.{clean_source_name} must be an object.")
        validated[clean_source_name] = dict(source_config)
    return validated


def _validate_endpoint(
    *,
    endpoint_id: object,
    endpoint_key: object,
    display_name: object,
    spec: object,
    source_configs: object,
    spec_hash: object,
    active: object,
) -> EndpointConfig:
    clean_endpoint_key = _clean_text(endpoint_key)
    clean_display_name = _clean_text(display_name)
    clean_spec_hash = _clean_text(spec_hash)
    if not clean_endpoint_key:
        raise EndpointConfigError("Endpoint endpoint_key is required.")
    if not clean_display_name:
        raise EndpointConfigError("Endpoint display_name is required.")
    if not clean_spec_hash:
        raise EndpointConfigError("Endpoint spec_hash is required.")

    validated_spec = _validate_mapping(spec, "spec")
    measurement = _validate_mapping(validated_spec.get("measurement"), "spec.measurement")
    measurement_type = _clean_text(measurement.get("type"))
    value_kind = _clean_text(measurement.get("value_kind"))
    if not measurement_type:
        raise EndpointConfigError("Endpoint spec.measurement.type is required.")
    if not value_kind:
        raise EndpointConfigError("Endpoint spec.measurement.value_kind is required.")
    if value_kind not in ALLOWED_VALUE_KINDS:
        allowed = ", ".join(sorted(ALLOWED_VALUE_KINDS))
        raise EndpointConfigError(
            f"Invalid endpoint spec.measurement.value_kind '{value_kind}'. Allowed: {allowed}."
        )

    return EndpointConfig(
        endpoint_id=int(endpoint_id),
        endpoint_key=clean_endpoint_key,
        display_name=clean_display_name,
        spec=validated_spec,
        source_configs=_validate_source_configs(source_configs),
        spec_hash=clean_spec_hash,
        active=bool(active),
    )


def _load_endpoint_from_cursor(
    cur: psycopg.Cursor,
    endpoint_key: str,
    *,
    include_inactive: bool,
) -> EndpointConfig:
    cur.execute(
        """
        SELECT
            endpoint_id,
            endpoint_key,
            display_name,
            spec,
            source_configs,
            spec_hash,
            active
        FROM endpoints
        WHERE endpoint_key = %s
        """,
        (endpoint_key,),
    )
    row = cur.fetchone()
    if row is None:
        raise EndpointNotFoundError(f"Endpoint '{endpoint_key}' was not found.")

    endpoint_id, row_key, display_name, spec, source_configs, spec_hash, active = row
    if not bool(active) and not include_inactive:
        raise InactiveEndpointError(f"Endpoint '{row_key}' is inactive.")

    return _validate_endpoint(
        endpoint_id=endpoint_id,
        endpoint_key=row_key,
        display_name=display_name,
        spec=spec,
        source_configs=source_configs,
        spec_hash=spec_hash,
        active=active,
    )


def load_endpoint(
    conn_or_cur: psycopg.Connection | psycopg.Cursor,
    endpoint_key: str,
    *,
    include_inactive: bool = False,
) -> EndpointConfig:
    clean_endpoint_key = _clean_text(endpoint_key)
    if not clean_endpoint_key:
        raise EndpointConfigError("endpoint_key is required.")

    if hasattr(conn_or_cur, "fetchone"):
        return _load_endpoint_from_cursor(
            conn_or_cur,
            clean_endpoint_key,
            include_inactive=include_inactive,
        )

    with conn_or_cur.cursor() as cur:
        return _load_endpoint_from_cursor(
            cur,
            clean_endpoint_key,
            include_inactive=include_inactive,
        )


def list_active_endpoints(conn_or_cur: psycopg.Connection | psycopg.Cursor) -> list[EndpointConfig]:
    def _list_from_cursor(cur: psycopg.Cursor) -> list[EndpointConfig]:
        cur.execute(
            """
            SELECT
                endpoint_id,
                endpoint_key,
                display_name,
                spec,
                source_configs,
                spec_hash,
                active
            FROM endpoints
            WHERE active
            ORDER BY display_name, endpoint_key
            """
        )
        return [
            _validate_endpoint(
                endpoint_id=row[0],
                endpoint_key=row[1],
                display_name=row[2],
                spec=row[3],
                source_configs=row[4],
                spec_hash=row[5],
                active=row[6],
            )
            for row in cur.fetchall()
        ]

    if hasattr(conn_or_cur, "fetchall"):
        return _list_from_cursor(conn_or_cur)

    with conn_or_cur.cursor() as cur:
        return _list_from_cursor(cur)


def get_source_config(endpoint: EndpointConfig, source_name: str) -> dict[str, Any]:
    clean_source_name = _clean_text(source_name).lower()
    if not clean_source_name:
        raise MissingSourceConfigError("source_name is required.")

    stored_source_name = None
    for candidate in endpoint.source_configs:
        if candidate.lower() == clean_source_name:
            stored_source_name = candidate
            break

    if stored_source_name is None:
        raise MissingSourceConfigError(
            f"Endpoint '{endpoint.endpoint_key}' has no source config for '{clean_source_name}'."
        )

    return dict(endpoint.source_configs[stored_source_name])
