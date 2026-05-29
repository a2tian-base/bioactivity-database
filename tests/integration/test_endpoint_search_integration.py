import os

import pytest
from psycopg.types.json import Json

from bioactivity.endpoint_search import search_saved_endpoints
from herg.config import DbConfig
from herg.db import get_conn


pytestmark = pytest.mark.skipif(os.getenv("HERG_TEST_DB") != "1", reason="set HERG_TEST_DB=1 to run")


INACTIVE_ENDPOINT_KEY = "endpoint_search_inactive_fixture"


def _cleanup(cur) -> None:
    cur.execute(
        """
        DELETE FROM endpoints
        WHERE endpoint_key = %s
        """,
        (INACTIVE_ENDPOINT_KEY,),
    )


def _insert_inactive_endpoint(cur) -> None:
    spec = {
        "target": {
            "preferred_name": "EGFR",
            "gene_symbol": "EGFR",
            "organism": "Homo sapiens",
        },
        "measurement": {
            "type": "IC50",
            "value_kind": "concentration",
        },
    }
    source_configs = {
        "chembl": {
            "target_chembl_id": "CHEMBL203",
            "standard_type": "IC50",
        }
    }
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
        VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, FALSE)
        ON CONFLICT (endpoint_key)
        DO UPDATE SET
            display_name = EXCLUDED.display_name,
            spec = EXCLUDED.spec,
            source_configs = EXCLUDED.source_configs,
            spec_hash = EXCLUDED.spec_hash,
            active = EXCLUDED.active
        """,
        (
            INACTIVE_ENDPOINT_KEY,
            "EGFR IC50",
            Json(spec),
            Json(source_configs),
            f"{INACTIVE_ENDPOINT_KEY}-hash",
        ),
    )


def test_search_saved_endpoints_finds_seeded_herg_endpoint():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        results = search_saved_endpoints(cur, "hERG IC50")

    assert [result.endpoint_key for result in results[:1]] == ["herg_ic50"]
    assert results[0].gene_symbol == "KCNH2"
    assert results[0].measurement_type == "IC50"
    assert results[0].source_names == ("chembl", "pubchem")


def test_search_saved_endpoints_finds_seeded_endpoint_by_gene_symbol():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        results = search_saved_endpoints(cur, "KCNH2")

    assert [result.endpoint_key for result in results[:1]] == ["herg_ic50"]


def test_search_saved_endpoints_respects_include_inactive():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        _cleanup(cur)
        _insert_inactive_endpoint(cur)

        assert search_saved_endpoints(cur, "EGFR human IC50") == []

        results = search_saved_endpoints(cur, "EGFR human IC50", include_inactive=True)

        assert [result.endpoint_key for result in results[:1]] == [INACTIVE_ENDPOINT_KEY]
        assert results[0].active is False

        _cleanup(cur)
        conn.commit()
