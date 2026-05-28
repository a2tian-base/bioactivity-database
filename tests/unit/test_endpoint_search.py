from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from bioactivity.endpoint_search import parse_endpoint_query, search_saved_endpoints


def _endpoint_rows():
    return [
        (
            1,
            "herg_ic50",
            "hERG IC50",
            {
                "target": {
                    "preferred_name": "hERG",
                    "gene_symbol": "KCNH2",
                    "organism": "Homo sapiens",
                },
                "measurement": {
                    "type": "IC50",
                    "value_kind": "concentration",
                },
                "inclusion_criteria": {
                    "organism": "Homo sapiens",
                },
            },
            {
                "chembl": {
                    "target_chembl_id": "CHEMBL240",
                    "standard_type": "IC50",
                },
                "pubchem": {
                    "target_gene_symbol": "KCNH2",
                    "target_gene_id": "3757",
                },
            },
            True,
        ),
        (
            2,
            "drd2_ki",
            "DRD2 Ki",
            {
                "target": {
                    "preferred_name": "Dopamine receptor D2",
                    "gene_symbol": "DRD2",
                    "organism": "Homo sapiens",
                },
                "measurement": {
                    "type": "Ki",
                    "value_kind": "concentration",
                },
            },
            {
                "chembl": {
                    "target_chembl_id": "CHEMBL217",
                    "standard_type": "Ki",
                }
            },
            True,
        ),
        (
            3,
            "cyp3a4_ic50",
            "CYP3A4 IC50",
            {
                "target": {
                    "preferred_name": "Cytochrome P450 3A4",
                    "gene_symbol": "CYP3A4",
                    "organism": "Homo sapiens",
                },
                "measurement": {
                    "type": "IC50",
                    "value_kind": "concentration",
                },
            },
            {
                "chembl": {
                    "target_chembl_id": "CHEMBL340",
                    "standard_type": "IC50",
                }
            },
            True,
        ),
        (
            4,
            "egfr_ic50",
            "EGFR IC50",
            {
                "target": {
                    "preferred_name": "Epidermal growth factor receptor",
                    "gene_symbol": "EGFR",
                    "organism": "Homo sapiens",
                },
                "measurement": {
                    "type": "IC50",
                    "value_kind": "concentration",
                },
            },
            {
                "chembl": {
                    "target_chembl_id": "CHEMBL203",
                    "standard_type": "IC50",
                }
            },
            False,
        ),
    ]


class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        self.executed.append((sql, params))

    def fetchall(self):
        return list(self._rows)


class _FakeConnection:
    def __init__(self, rows):
        self.rows = list(rows)
        self.cursors: list[_FakeCursor] = []

    @contextmanager
    def cursor(self) -> Iterator[_FakeCursor]:
        cursor = _FakeCursor(self.rows)
        self.cursors.append(cursor)
        yield cursor


def test_parse_endpoint_query_for_herg_ic50():
    parsed = parse_endpoint_query("hERG IC50")

    assert parsed.raw_query == "hERG IC50"
    assert parsed.target_query == "hERG"
    assert parsed.measurement_types == ("IC50",)
    assert parsed.organism is None


def test_parse_endpoint_query_for_drd2_ki():
    parsed = parse_endpoint_query("DRD2 Ki")

    assert parsed.target_query == "DRD2"
    assert parsed.measurement_types == ("Ki",)
    assert parsed.organism is None


def test_parse_endpoint_query_for_cyp3a4_inhibition():
    parsed = parse_endpoint_query("CYP3A4 inhibition")

    assert parsed.target_query == "CYP3A4"
    assert parsed.measurement_types == ("inhibition",)
    assert parsed.organism is None


def test_parse_endpoint_query_for_percent_inhibition():
    parsed = parse_endpoint_query("hERG percent inhibition")

    assert parsed.target_query == "hERG"
    assert parsed.measurement_types == ("percent inhibition",)
    assert parsed.organism is None


def test_parse_endpoint_query_for_human_ic50():
    parsed = parse_endpoint_query("EGFR human IC50")

    assert parsed.raw_query == "EGFR human IC50"
    assert parsed.target_query == "EGFR"
    assert parsed.measurement_types == ("IC50",)
    assert parsed.organism == "Homo sapiens"


def test_saved_endpoint_search_finds_by_endpoint_key():
    conn = _FakeConnection(_endpoint_rows())

    results = search_saved_endpoints(conn, "drd2_ki")

    assert [result.endpoint_key for result in results] == ["drd2_ki"]


def test_saved_endpoint_search_finds_by_display_name():
    results = search_saved_endpoints(_FakeCursor(_endpoint_rows()), "DRD2 Ki")

    assert [result.endpoint_key for result in results] == ["drd2_ki"]


def test_saved_endpoint_search_finds_by_target_gene_symbol_in_spec():
    results = search_saved_endpoints(_FakeCursor(_endpoint_rows()), "KCNH2")

    assert [result.endpoint_key for result in results] == ["herg_ic50"]
    assert results[0].gene_symbol == "KCNH2"
    assert results[0].target_name == "hERG"


def test_saved_endpoint_search_respects_inactive_flag():
    rows = _endpoint_rows()

    assert search_saved_endpoints(_FakeCursor(rows), "EGFR IC50") == []

    inactive_results = search_saved_endpoints(_FakeCursor(rows), "EGFR IC50", include_inactive=True)

    assert [result.endpoint_key for result in inactive_results] == ["egfr_ic50"]
    assert inactive_results[0].active is False


def test_saved_endpoint_search_includes_source_names():
    results = search_saved_endpoints(_FakeCursor(_endpoint_rows()), "hERG IC50")

    assert [result.endpoint_key for result in results] == ["herg_ic50"]
    assert results[0].measurement_type == "IC50"
    assert results[0].value_kind == "concentration"
    assert results[0].organism == "Homo sapiens"
    assert results[0].source_names == ("chembl", "pubchem")


def test_saved_endpoint_search_uses_parsed_human_filter():
    results = search_saved_endpoints(_FakeCursor(_endpoint_rows()), "EGFR human IC50", include_inactive=True)

    assert [result.endpoint_key for result in results] == ["egfr_ic50"]
