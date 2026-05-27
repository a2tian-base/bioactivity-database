import os
from decimal import Decimal

import pytest

from herg.config import DbConfig
from herg.db import get_conn, upsert_compound, upsert_ic50_result, upsert_source_record
from herg.models import CompoundInput, Ic50Input, SourceRecordInput
from herg.normalize import build_identifier_inputs


pytestmark = pytest.mark.skipif(os.getenv("HERG_TEST_DB") != "1", reason="set HERG_TEST_DB=1 to run")


BASELINE_SOURCE_NAMES = (
    "baseline_ic50_generated",
    "baseline_source_upsert",
)
BASELINE_INCHIKEYS = (
    "BASELINEIC50GENERATED-UHFFFAOYSA-N",
    "BASELINEIDENTIFIERS-UHFFFAOYSA-N",
)


def _cleanup(cur):
    cur.execute(
        """
        DELETE FROM ic50_results
        WHERE source_record_id IN (
            SELECT source_record_id
            FROM source_records
            WHERE source_name = ANY(%s)
        )
        """,
        (list(BASELINE_SOURCE_NAMES),),
    )
    cur.execute(
        """
        DELETE FROM source_records
        WHERE source_name = ANY(%s)
        """,
        (list(BASELINE_SOURCE_NAMES),),
    )
    cur.execute(
        """
        DELETE FROM compounds
        WHERE standard_inchikey = ANY(%s)
           OR compound_id IN (
               SELECT compound_id
               FROM compound_identifiers
               WHERE namespace = 'baseline_ic50'
                  OR (namespace = 'pubchem_cid' AND identifier_value = '900000001')
                  OR (namespace = 'chembl_id' AND identifier_value = 'CHEMBLBASELINE001')
           )
        """,
        (list(BASELINE_INCHIKEYS),),
    )


def _store_ic50(cur, compound_id: int, key: str, value: float, unit: str, qualifier: str) -> dict:
    source_record_id = upsert_source_record(
        cur,
        SourceRecordInput(
            source_name="baseline_ic50_generated",
            source_record_key=key,
            record_type="fixture",
            raw_payload={"source_record_key": key},
        ),
    )
    return upsert_ic50_result(
        cur,
        compound_id,
        source_record_id,
        Ic50Input(
            ic50_value=value,
            ic50_unit=unit,
            qualifier=qualifier,
            endpoint="IC50",
        ),
    )


def test_ic50_generated_unit_pic50_and_qualifier_semantics():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        _cleanup(cur)
        compound_id = upsert_compound(
            cur,
            CompoundInput(
                standard_inchikey="BASELINEIC50GENERATED-UHFFFAOYSA-N",
                identifiers=build_identifier_inputs({"baseline_ic50": "generated"}, "baseline_ic50"),
            ),
        )

        unit_cases = [
            ("unit:pm", 1.0, "pM", "=", Decimal("0.000001"), Decimal("12.0000"), "="),
            ("unit:nm", 1.0, "nM", "<", Decimal("0.001000"), Decimal("9.0000"), ">"),
            ("unit:um", 1.0, "uM", ">", Decimal("1.000000"), Decimal("6.0000"), "<"),
            ("unit:mm", 1.0, "mM", "=", Decimal("1000.000000"), Decimal("3.0000"), "="),
        ]
        for key, value, unit, qualifier, expected_um, expected_pic50, expected_pic50_qualifier in unit_cases:
            result = _store_ic50(cur, compound_id, key, value, unit, qualifier)
            assert result["ic50_um"] == expected_um
            assert result["pic50"] == expected_pic50
            assert result["pic50_qualifier"] == expected_pic50_qualifier

        pic50_cases = [
            ("pic50:1um", 1.0, "uM", Decimal("6.0000")),
            ("pic50:100nm", 100.0, "nM", Decimal("7.0000")),
            ("pic50:10um", 10.0, "uM", Decimal("5.0000")),
        ]
        for key, value, unit, expected_pic50 in pic50_cases:
            result = _store_ic50(cur, compound_id, key, value, unit, "=")
            assert result["pic50"] == expected_pic50

        _cleanup(cur)
        conn.commit()


def test_source_record_upsert_preserves_existing_optional_values_and_empty_payload():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        _cleanup(cur)
        first_id = upsert_source_record(
            cur,
            SourceRecordInput(
                source_name="baseline_source_upsert",
                source_record_key="source:1",
                record_type="initial",
                source_release="release-1",
                source_url="https://example.org/initial",
                raw_payload={"version": 1},
            ),
        )
        second_id = upsert_source_record(
            cur,
            SourceRecordInput(
                source_name="baseline_source_upsert",
                source_record_key="source:1",
                record_type="updated",
                source_release="",
                source_url="https://example.org/updated",
                raw_payload={},
            ),
        )

        cur.execute(
            """
            SELECT COUNT(*), MIN(record_type), MIN(source_release), MIN(source_url), MIN(raw_payload::text)
            FROM source_records
            WHERE source_name = 'baseline_source_upsert'
              AND source_record_key = 'source:1'
            """
        )
        count, record_type, source_release, source_url, raw_payload = cur.fetchone()

        assert second_id == first_id
        assert count == 1
        assert record_type == "updated"
        assert source_release == "release-1"
        assert source_url == "https://example.org/updated"
        assert raw_payload == '{"version": 1}'

        _cleanup(cur)
        conn.commit()


def test_compound_identifier_resolution_for_current_identifier_types():
    db_config = DbConfig.from_env()

    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        _cleanup(cur)
        compound_id = upsert_compound(
            cur,
            CompoundInput(
                standard_inchikey="BASELINEIDENTIFIERS-UHFFFAOYSA-N",
                identifiers=build_identifier_inputs(
                    {
                        "pubchem_cid": "900000001",
                        "chembl_id": "CHEMBLBASELINE001",
                    },
                    "chembl_id",
                ),
            ),
        )

        for id_type, id_value in [
            ("pubchem_cid", "900000001"),
            ("chembl_id", "chemblbaseline001"),
            ("standard_inchikey", "baselineidentifiers-uhfffaoysa-n"),
        ]:
            cur.execute("SELECT resolve_compound_id(%s, %s)", (id_type, id_value))
            assert cur.fetchone()[0] == compound_id

        _cleanup(cur)
        conn.commit()
