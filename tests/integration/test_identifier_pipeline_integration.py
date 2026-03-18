import json
import os
from pathlib import Path

import pytest

from herg.config import DbConfig, IdentifierRunConfig
from herg.db import get_conn, upsert_compound
from herg.models import CompoundInput
from herg.normalize import build_identifier_inputs, build_name_inputs
from herg.identifier_pipeline import run_identifier_pipeline
from herg.sources.identifiers_csv import IdentifiersCsvAdapter


@pytest.mark.skipif(os.getenv("HERG_TEST_DB") != "1", reason="set HERG_TEST_DB=1 to run")
def test_identifier_pipeline_handles_attach_idempotence_and_conflicts(tmp_path: Path):
    db_config = DbConfig.from_env()
    source_name = "identifier_csv_test"
    fixture_namespace = "fixture_enrichment_test"
    match_inchikey = "TSTENRICH00001-UHFFFAOYSA-N"
    conflict_inchikey = "TSTENRICH00002-UHFFFAOYSA-N"

    def cleanup() -> None:
        with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM compounds
                WHERE compound_id IN (
                    SELECT compound_id
                    FROM compound_identifiers
                    WHERE namespace = %s
                )
                """,
                (fixture_namespace,),
            )
            cur.execute(
                "DELETE FROM source_records WHERE source_name = %s",
                (source_name,),
            )
            conn.commit()

    cleanup()

    try:
        with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
            upsert_compound(
                cur,
                CompoundInput(
                    standard_inchikey=match_inchikey,
                    identifiers=build_identifier_inputs(
                        {
                            fixture_namespace: "fixture-a",
                            "pubchem_cid": "97002",
                        },
                        fixture_namespace,
                    ),
                    names=build_name_inputs(preferred_name="Fixture A"),
                ),
            )
            upsert_compound(
                cur,
                CompoundInput(
                    standard_inchikey=conflict_inchikey,
                    identifiers=build_identifier_inputs(
                        {
                            fixture_namespace: "fixture-b",
                            "unii": "CONFLICT-001",
                        },
                        fixture_namespace,
                    ),
                    names=build_name_inputs(preferred_name="Fixture B"),
                ),
            )
            conn.commit()

        csv_path = tmp_path / "identifier_enrichment.csv"
        csv_path.write_text(
            "match_inchikey,match_pubchem_cid,match_name,add_namespace,add_value,is_primary,source_record_key\n"
            f"{match_inchikey},,,unii,UNII-001,true,row-1\n"
            ",97002,,a_number,A-100,true,row-2\n"
            f"{match_inchikey},,,unii,UNII-001,true,row-3\n"
            ",,,unii,UNMATCHED-001,false,row-4\n"
            f"{match_inchikey},,,unii,CONFLICT-001,false,row-5\n"
            ",,Fixture A,a_number,NAME-ONLY,false,row-6\n",
            encoding="utf-8",
        )

        errors_path = tmp_path / "errors.jsonl"
        unmatched_path = tmp_path / "unmatched.jsonl"
        conflicts_path = tmp_path / "conflicts.jsonl"
        stats_path = tmp_path / "stats.json"

        adapter = IdentifiersCsvAdapter(csv_path=csv_path, source_name=source_name)
        run_config = IdentifierRunConfig(
            dry_run=False,
            commit_every=1,
            errors_path=str(errors_path),
            stats_path=str(stats_path),
            unmatched_path=str(unmatched_path),
            conflicts_path=str(conflicts_path),
        )

        first_stats = run_identifier_pipeline(adapter, db_config, run_config)
        second_stats = run_identifier_pipeline(adapter, db_config, run_config)

        assert first_stats.processed == 6
        assert first_stats.attached == 2
        assert first_stats.already_present == 1
        assert first_stats.unmatched == 1
        assert first_stats.conflict == 1
        assert first_stats.skipped_invalid == 1
        assert first_stats.failed == 0

        assert second_stats.processed == 6
        assert second_stats.attached == 0
        assert second_stats.already_present == 3
        assert second_stats.unmatched == 1
        assert second_stats.conflict == 1
        assert second_stats.skipped_invalid == 1
        assert second_stats.failed == 0

        with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT compound_id
                FROM compounds
                WHERE UPPER(BTRIM(standard_inchikey)) = UPPER(%s)
                """,
                (match_inchikey,),
            )
            compound_id = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*)
                FROM compound_identifiers
                WHERE compound_id = %s
                  AND namespace = 'unii'
                  AND normalized_value = normalize_identifier('unii', %s)
                """,
                (compound_id, "UNII-001"),
            )
            assert cur.fetchone()[0] == 1

            cur.execute(
                """
                SELECT COUNT(*)
                FROM compound_identifiers
                WHERE compound_id = %s
                  AND namespace = 'a_number'
                  AND normalized_value = normalize_identifier('a_number', %s)
                """,
                (compound_id, "A-100"),
            )
            assert cur.fetchone()[0] == 1

            cur.execute(
                """
                SELECT COUNT(*)
                FROM compound_identifier_sources cis
                JOIN compound_identifiers ci
                  ON ci.compound_identifier_id = cis.compound_identifier_id
                JOIN source_records sr
                  ON sr.source_record_id = cis.source_record_id
                WHERE ci.compound_id = %s
                  AND ci.namespace = 'unii'
                  AND ci.normalized_value = normalize_identifier('unii', %s)
                  AND sr.source_name = %s
                """,
                (compound_id, "UNII-001", source_name),
            )
            assert cur.fetchone()[0] == 2

            cur.execute(
                "SELECT COUNT(*) FROM source_records WHERE source_name = %s",
                (source_name,),
            )
            assert cur.fetchone()[0] == 4

        unmatched_lines = unmatched_path.read_text(encoding="utf-8").strip().splitlines()
        conflict_lines = conflicts_path.read_text(encoding="utf-8").strip().splitlines()
        error_lines = errors_path.read_text(encoding="utf-8").strip().splitlines()

        assert len(unmatched_lines) == 2
        assert len(conflict_lines) == 2
        assert len(error_lines) == 2
        assert any("UNMATCHED-001" in line for line in unmatched_lines)
        assert any("CONFLICT-001" in line for line in conflict_lines)
        assert all("Name-based matching is not supported" in line for line in error_lines)

        stats_payload = json.loads(stats_path.read_text(encoding="utf-8"))
        assert stats_payload["already_present"] == 3
        assert stats_payload["conflict"] == 1
    finally:
        cleanup()
