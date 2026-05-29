import os

import pytest
from psycopg.types.json import Json

from bioactivity.endpoints import (
    EndpointConfigError,
    EndpointNotFoundError,
    InactiveEndpointError,
    MissingSourceConfigError,
    get_source_config,
    load_endpoint,
)
from herg.config import DbConfig
from herg.db import get_conn


pytestmark = pytest.mark.skipif(os.getenv("HERG_TEST_DB") != "1", reason="set HERG_TEST_DB=1 to run")


INACTIVE_ENDPOINT_KEY = "endpoint_config_inactive_fixture"
INVALID_VALUE_KIND_ENDPOINT_KEY = "endpoint_config_invalid_value_kind_fixture"
INVALID_SOURCE_CONFIG_ENDPOINT_KEY = "endpoint_config_invalid_source_config_fixture"
FIXTURE_ENDPOINT_KEYS = (
    INACTIVE_ENDPOINT_KEY,
    INVALID_VALUE_KIND_ENDPOINT_KEY,
    INVALID_SOURCE_CONFIG_ENDPOINT_KEY,
)


def _cleanup(cur):
    cur.execute(
        """
        DELETE FROM endpoints
        WHERE endpoint_key IN (%s, %s, %s)
        """,
        FIXTURE_ENDPOINT_KEYS,
    )


def _valid_spec(value_kind: str = "concentration") -> dict[str, object]:
    return {
        "measurement": {
            "type": "IC50",
            "value_kind": value_kind,
        }
    }


def _upsert_endpoint(
    cur,
    endpoint_key: str,
    *,
    spec: dict[str, object] | list[object] | None = None,
    source_configs: dict[str, object] | None = None,
    active: bool = True,
) -> int:
    cur.execute(
        """
        INSERT INTO endpoints (
            endpoint_key,
            display_name,
            spec,
            source_configs,
            spec_hash,
            active
        )
        VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s)
        ON CONFLICT (endpoint_key)
        DO UPDATE SET
            display_name = EXCLUDED.display_name,
            spec = EXCLUDED.spec,
            source_configs = EXCLUDED.source_configs,
            spec_hash = EXCLUDED.spec_hash,
            active = EXCLUDED.active
        RETURNING endpoint_id
        """,
        (
            endpoint_key,
            endpoint_key.replace("_", " ").title(),
            Json(spec or _valid_spec()),
            Json(source_configs if source_configs is not None else {}),
            f"{endpoint_key}-hash",
            active,
        ),
    )
    return int(cur.fetchone()[0])


def test_load_seeded_herg_endpoint_validates_and_returns_source_configs():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        endpoint = load_endpoint(cur, " herg_ic50 ")

        assert endpoint.endpoint_key == "herg_ic50"
        assert endpoint.display_name == "hERG IC50"
        assert endpoint.active is True
        assert endpoint.spec["measurement"]["type"] == "IC50"
        assert endpoint.spec["measurement"]["value_kind"] == "concentration"
        assert get_source_config(endpoint, "ChEMBL")["target_chembl_id"] == "CHEMBL240"
        assert endpoint.source_config("pubchem")["target_gene_id"] == "3757"


def test_load_seeded_cyp3a4_endpoint_uses_generic_ic50_config():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        endpoint = load_endpoint(cur, "cyp3a4_ic50")
        cur.execute("SELECT to_regclass('cyp3a4_ic50_results')")
        endpoint_specific_table = cur.fetchone()[0]

        assert endpoint.endpoint_key == "cyp3a4_ic50"
        assert endpoint.display_name == "CYP3A4 IC50"
        assert endpoint.active is True
        assert endpoint.spec["measurement"]["type"] == "IC50"
        assert endpoint.spec["measurement"]["value_kind"] == "concentration"
        assert endpoint.spec["target"]["gene_symbol"] == "CYP3A4"
        assert get_source_config(endpoint, "chembl")["target_chembl_id"] == "CHEMBL340"
        assert endpoint.source_config("pubchem")["target_gene_id"] == "1576"
        assert endpoint_specific_table is None


def test_load_endpoint_accepts_connection_objects():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn:
        endpoint = load_endpoint(conn, "herg_ic50")

        assert endpoint.endpoint_key == "herg_ic50"


def test_load_unknown_endpoint_raises_clear_error():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        with pytest.raises(EndpointNotFoundError, match="was not found"):
            load_endpoint(cur, "does_not_exist")


def test_load_inactive_endpoint_requires_explicit_opt_in():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        _cleanup(cur)
        _upsert_endpoint(cur, INACTIVE_ENDPOINT_KEY, active=False)

        with pytest.raises(InactiveEndpointError, match="is inactive"):
            load_endpoint(cur, INACTIVE_ENDPOINT_KEY)

        endpoint = load_endpoint(cur, INACTIVE_ENDPOINT_KEY, include_inactive=True)

        assert endpoint.endpoint_key == INACTIVE_ENDPOINT_KEY
        assert endpoint.active is False

        _cleanup(cur)
        conn.commit()


def test_load_endpoint_with_invalid_measurement_value_kind_raises_validation_error():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        _cleanup(cur)
        _upsert_endpoint(
            cur,
            INVALID_VALUE_KIND_ENDPOINT_KEY,
            spec=_valid_spec(value_kind="boolean"),
        )

        with pytest.raises(EndpointConfigError, match="value_kind 'boolean'"):
            load_endpoint(cur, INVALID_VALUE_KIND_ENDPOINT_KEY)

        _cleanup(cur)
        conn.commit()


def test_load_endpoint_with_non_object_source_config_raises_validation_error():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        _cleanup(cur)
        _upsert_endpoint(
            cur,
            INVALID_SOURCE_CONFIG_ENDPOINT_KEY,
            source_configs={"chembl": []},
        )

        with pytest.raises(EndpointConfigError, match="source_configs.chembl must be an object"):
            load_endpoint(cur, INVALID_SOURCE_CONFIG_ENDPOINT_KEY)

        _cleanup(cur)
        conn.commit()


def test_requesting_missing_source_config_raises_clear_error():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        endpoint = load_endpoint(cur, "herg_ic50")

        with pytest.raises(MissingSourceConfigError, match="no source config for 'unichem'"):
            get_source_config(endpoint, "unichem")
