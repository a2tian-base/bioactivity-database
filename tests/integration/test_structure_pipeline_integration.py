import os
import pytest

from herg.config import DbConfig, StructureRunConfig
from herg.db import get_conn, upsert_compound
from herg.models import CompoundInput, CompoundMatchInput, SourceRecordInput, StructureEnrichmentRecord, StructureInput
from herg.normalize import build_identifier_inputs, build_name_inputs
from herg.structure_pipeline import run_structure_pipeline


FIXTURE_NAMESPACE = "fixture_structure_test"


def _cleanup(db_config: DbConfig, source_names: list[str]) -> None:
    with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
        if source_names:
            cur.execute(
                "DELETE FROM source_records WHERE source_name = ANY(%s)",
                (source_names,),
            )
        cur.execute(
            """
            DELETE FROM compounds
            WHERE compound_id IN (
                SELECT compound_id
                FROM compound_identifiers
                WHERE namespace = %s
            )
            """,
            (FIXTURE_NAMESPACE,),
        )
        conn.commit()


class _FixtureAdapter:
    def __init__(self, rows):
        self.rows = rows
        self.source_name = "fixture_structure"
        self.enrich_batch_size = 50

    def iter_raw_rows(self):
        yield from self.rows

    def enrich_batch(self, rows):
        return rows

    def map_row(self, row):
        return row["record"]


def _fetch_compound(cur, seed_value: str):
    cur.execute(
        """
        SELECT c.compound_id, c.canonical_smiles, c.standard_inchi, c.standard_inchikey
        FROM compounds c
        JOIN compound_identifiers ci
          ON ci.compound_id = c.compound_id
        WHERE ci.namespace = %s
          AND ci.normalized_value = normalize_identifier(%s, %s)
        """,
        (FIXTURE_NAMESPACE, FIXTURE_NAMESPACE, seed_value),
    )
    return cur.fetchone()


@pytest.mark.skipif(os.getenv("HERG_TEST_DB") != "1", reason="set HERG_TEST_DB=1 to run")
def test_structure_pipeline_chembl_backfills_missing_fields_and_is_idempotent():
    db_config = DbConfig.from_env()
    source_name = "structure_fixture_chembl"
    seed_value = "chembl-seed-a"
    chembl_id = "CHEMBL_STRUCT_FIXTURE_1"

    _cleanup(db_config, [source_name])

    try:
        with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
            upsert_compound(
                cur,
                CompoundInput(
                    canonical_smiles="CCC",
                    identifiers=build_identifier_inputs(
                        {
                            FIXTURE_NAMESPACE: seed_value,
                            "chembl_id": chembl_id,
                        },
                        FIXTURE_NAMESPACE,
                    ),
                    names=build_name_inputs(preferred_name="Structure Fixture A"),
                ),
            )
            conn.commit()

        record = StructureEnrichmentRecord(
            external_key=f"compound:{seed_value}|provider:chembl:structure",
            match=CompoundMatchInput(
                identifiers=build_identifier_inputs(
                    {
                        FIXTURE_NAMESPACE: seed_value,
                        "chembl_id": chembl_id,
                    },
                    FIXTURE_NAMESPACE,
                )
            ),
            structure=StructureInput(
                canonical_smiles="CCO",
                standard_inchi="InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3",
                standard_inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
            ),
            source_record=SourceRecordInput(
                source_name=source_name,
                source_record_key=f"molecule:{chembl_id}",
                record_type="structure_enrichment",
            ),
        )

        adapter = _FixtureAdapter([{"record": record, "external_key": record.external_key}])
        run_config = StructureRunConfig(dry_run=False, commit_every=1)

        first_stats = run_structure_pipeline(adapter, db_config, run_config)
        second_stats = run_structure_pipeline(adapter, db_config, run_config)

        assert first_stats.processed == 1
        assert first_stats.attached == 1
        assert first_stats.already_present == 0
        assert first_stats.conflict == 0
        assert second_stats.processed == 1
        assert second_stats.attached == 0
        assert second_stats.already_present == 1

        with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
            compound_id, canonical_smiles, standard_inchi, standard_inchikey = _fetch_compound(cur, seed_value)
            assert canonical_smiles == "CCC"
            assert standard_inchi == "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3"
            assert standard_inchikey == "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"

            cur.execute("SELECT COUNT(*) FROM source_records WHERE source_name = %s", (source_name,))
            assert cur.fetchone()[0] == 1

            cur.execute(
                """
                SELECT COUNT(*)
                FROM compound_structure_assertions csa
                JOIN source_records sr
                  ON sr.source_record_id = csa.source_record_id
                WHERE csa.compound_id = %s
                  AND sr.source_name = %s
                """,
                (compound_id, source_name),
            )
            assert cur.fetchone()[0] == 1
    finally:
        _cleanup(db_config, [source_name])


@pytest.mark.skipif(os.getenv("HERG_TEST_DB") != "1", reason="set HERG_TEST_DB=1 to run")
def test_structure_pipeline_pubchem_backfills_identity_fields_only():
    db_config = DbConfig.from_env()
    source_name = "structure_fixture_pubchem"
    seed_value = "pubchem-seed-a"
    pubchem_cid = "880001"

    _cleanup(db_config, [source_name])

    try:
        with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
            upsert_compound(
                cur,
                CompoundInput(
                    canonical_smiles="KEEP-ME",
                    identifiers=build_identifier_inputs(
                        {
                            FIXTURE_NAMESPACE: seed_value,
                            "pubchem_cid": pubchem_cid,
                        },
                        FIXTURE_NAMESPACE,
                    ),
                    names=build_name_inputs(preferred_name="Structure Fixture B"),
                ),
            )
            conn.commit()

        record = StructureEnrichmentRecord(
            external_key=f"compound:{seed_value}|provider:pubchem:structure",
            match=CompoundMatchInput(
                identifiers=build_identifier_inputs(
                    {
                        FIXTURE_NAMESPACE: seed_value,
                        "pubchem_cid": pubchem_cid,
                    },
                    FIXTURE_NAMESPACE,
                )
            ),
            structure=StructureInput(
                standard_inchi="InChI=1S/C7H8N4O2/c1-4-7(13)11-6(12)10-5(4)8-2-3-9/h2-3H,1H3,(H3,8,10,11,12,13)",
                standard_inchikey="RYYVLZVUVIJVGH-UHFFFAOYSA-N",
            ),
            source_record=SourceRecordInput(
                source_name=source_name,
                source_record_key=f"cid:{pubchem_cid}|properties:structure",
                record_type="structure_enrichment",
            ),
        )

        adapter = _FixtureAdapter([{"record": record, "external_key": record.external_key}])
        stats = run_structure_pipeline(adapter, db_config, StructureRunConfig(dry_run=False, commit_every=1))

        assert stats.processed == 1
        assert stats.attached == 1
        assert stats.conflict == 0

        with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
            _, canonical_smiles, standard_inchi, standard_inchikey = _fetch_compound(cur, seed_value)
            assert canonical_smiles == "KEEP-ME"
            assert standard_inchi.startswith("InChI=1S/C7H8N4O2")
            assert standard_inchikey == "RYYVLZVUVIJVGH-UHFFFAOYSA-N"
    finally:
        _cleanup(db_config, [source_name])


@pytest.mark.skipif(os.getenv("HERG_TEST_DB") != "1", reason="set HERG_TEST_DB=1 to run")
def test_structure_pipeline_rejects_conflicting_inchikey(tmp_path):
    db_config = DbConfig.from_env()
    source_name = "structure_fixture_conflict"
    seed_value = "chembl-seed-conflict"
    chembl_id = "CHEMBL_STRUCT_FIXTURE_CONFLICT"
    conflicts_path = tmp_path / "structure_conflicts.jsonl"

    _cleanup(db_config, [source_name])

    try:
        with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
            upsert_compound(
                cur,
                CompoundInput(
                    standard_inchikey="AAAAAAAABBBBBB-UHFFFAOYSA-N",
                    identifiers=build_identifier_inputs(
                        {
                            FIXTURE_NAMESPACE: seed_value,
                            "chembl_id": chembl_id,
                        },
                        FIXTURE_NAMESPACE,
                    ),
                    names=build_name_inputs(preferred_name="Structure Fixture Conflict"),
                ),
            )
            conn.commit()

        record = StructureEnrichmentRecord(
            external_key=f"compound:{seed_value}|provider:chembl:structure",
            match=CompoundMatchInput(
                identifiers=build_identifier_inputs(
                    {
                        FIXTURE_NAMESPACE: seed_value,
                        "chembl_id": chembl_id,
                    },
                    FIXTURE_NAMESPACE,
                )
            ),
            structure=StructureInput(
                standard_inchi="InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3",
                standard_inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
            ),
            source_record=SourceRecordInput(
                source_name=source_name,
                source_record_key=f"molecule:{chembl_id}",
                record_type="structure_enrichment",
            ),
        )

        adapter = _FixtureAdapter([{"record": record, "external_key": record.external_key}])
        stats = run_structure_pipeline(
            adapter,
            db_config,
            StructureRunConfig(dry_run=False, commit_every=1, conflicts_path=str(conflicts_path)),
        )

        assert stats.processed == 1
        assert stats.attached == 0
        assert stats.conflict == 1
        assert stats.failed == 0

        with get_conn(db_config=db_config) as conn, conn.cursor() as cur:
            _, _, standard_inchi, standard_inchikey = _fetch_compound(cur, seed_value)
            assert standard_inchi in (None, "")
            assert standard_inchikey == "AAAAAAAABBBBBB-UHFFFAOYSA-N"

            cur.execute("SELECT COUNT(*) FROM source_records WHERE source_name = %s", (source_name,))
            assert cur.fetchone()[0] == 0

            cur.execute(
                """
                SELECT COUNT(*)
                FROM compound_structure_assertions csa
                JOIN compounds c
                  ON c.compound_id = csa.compound_id
                JOIN compound_identifiers ci
                  ON ci.compound_id = c.compound_id
                WHERE ci.namespace = %s
                  AND ci.normalized_value = normalize_identifier(%s, %s)
                """,
                (FIXTURE_NAMESPACE, FIXTURE_NAMESPACE, seed_value),
            )
            assert cur.fetchone()[0] == 0

        conflict_lines = conflicts_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(conflict_lines) == 1
        assert "Incoming standard_inchikey conflicts with the existing compound structure." in conflict_lines[0]
    finally:
        _cleanup(db_config, [source_name])
