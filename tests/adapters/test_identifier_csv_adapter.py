from pathlib import Path

from herg.sources.identifiers_csv import IdentifiersCsvAdapter


def test_identifier_csv_adapter_maps_row(tmp_path: Path):
    csv_path = tmp_path / "identifier_enrichment.csv"
    csv_path.write_text(
        "match_inchikey,match_chembl_id,match_pubchem_cid,add_namespace,add_value,is_primary,source_record_key\n"
        "LFQSCWFLJHTTHZ-UHFFFAOYSA-N,CHEMBL545,702,unii,3K9958V90M,true,row-001\n",
        encoding="utf-8",
    )

    adapter = IdentifiersCsvAdapter(csv_path=csv_path)
    raw_rows = list(adapter.iter_raw_rows())
    assert len(raw_rows) == 1

    staged = adapter.map_row(raw_rows[0])
    assert staged.external_key == "row-001"
    assert staged.match.standard_inchikey == "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
    assert {identifier.namespace for identifier in staged.match.identifiers} == {"chembl_id", "pubchem_cid"}
    assert staged.identifiers_to_add[0].namespace == "unii"
    assert staged.identifiers_to_add[0].is_primary is True
    assert staged.source_record.source_name == "identifier_csv"
    assert staged.source_record.record_type == "identifier_enrichment"
