from herg.config import DbConfig, IdentifierRunConfig
from herg.identifier_pipeline import run_identifier_pipeline


class _DummyCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, *_args, **_kwargs):
        return None


class _DummyConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _DummyCursor(self)

    def commit(self):
        return None


class _PreclassifiedAdapter:
    source_name = "fixture"
    enrich_batch_size = 10

    def iter_raw_rows(self):
        yield {"external_key": "row:1"}
        yield {"external_key": "row:2"}
        yield {"external_key": "row:3"}

    def enrich_batch(self, rows):
        return [
            {
                **rows[0],
                "harvest_status": "unmatched",
                "harvest_reason": "No exact UniChem key.",
            },
            {
                **rows[1],
                "harvest_status": "conflict",
                "harvest_reason": "Multiple target identifiers.",
            },
            {
                **rows[2],
                "harvest_status": "error",
                "harvest_reason": "Remote lookup failed.",
            },
        ]

    def map_row(self, row):
        raise AssertionError(f"map_row should not be called for preclassified row {row!r}")


def test_identifier_pipeline_handles_preclassified_unmatched_conflict_and_error(monkeypatch):
    monkeypatch.setattr("herg.identifier_pipeline.get_conn", lambda **kwargs: _DummyConnection())
    monkeypatch.setattr("herg.identifier_pipeline.ensure_identifier_enrichment_schema", lambda cur: None)

    stats = run_identifier_pipeline(
        _PreclassifiedAdapter(),
        DbConfig(host="localhost", port=5432, dbname="herg", user="herg_user", password="change_me"),
        IdentifierRunConfig(dry_run=True),
    )

    assert stats.processed == 3
    assert stats.attached == 0
    assert stats.already_present == 0
    assert stats.unmatched == 1
    assert stats.conflict == 1
    assert stats.failed == 1
    assert stats.skipped_invalid == 0
