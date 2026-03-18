from herg.config import HttpConfig
from herg.sources.unichem_identifiers import UniChemIdentifierAdapter


def test_unichem_adapter_builds_candidate_record_for_unique_mapping(monkeypatch):
    monkeypatch.setattr(
        "herg.sources.unichem_identifiers.fetch_identifier_enrichment_candidates",
        lambda target_namespace, limit=None, db_config=None: [
            {
                "compound_id": 1,
                "standard_inchikey": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
                "chembl_id": "CHEMBL545",
                "pubchem_cid": None,
                "unii": "",
                "preferred_name": "ethanol",
            }
        ],
    )

    captured_payloads: list[dict] = []

    def fake_post_json(url, payload, config, label="UniChem"):
        captured_payloads.append(payload)
        return {
            "response": "Success",
            "compounds": [
                {
                    "sources": [
                        {"id": 1, "compoundId": "CHEMBL545"},
                        {"id": 22, "compoundId": "702"},
                    ]
                }
            ],
        }

    monkeypatch.setattr("herg.sources.unichem_identifiers.post_json", fake_post_json)

    adapter = UniChemIdentifierAdapter(
        http_config=HttpConfig(request_timeout_seconds=1, http_retries=0),
        target_namespace="pubchem_cid",
    )

    raw_rows = list(adapter.iter_raw_rows())
    assert adapter.last_candidate_count == 1
    enriched_rows = adapter.enrich_batch(raw_rows)
    row = enriched_rows[0]

    assert captured_payloads == [{"compound": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N", "type": "inchikey"}]
    assert row["harvest_status"] == "candidate"
    assert row["target_values"] == ["702"]

    record = adapter.map_row(row)
    csv_row = adapter.record_to_csv_row(record, row)

    assert record.identifiers_to_add[0].namespace == "pubchem_cid"
    assert record.identifiers_to_add[0].value == "702"
    assert csv_row["match_chembl_id"] == "CHEMBL545"
    assert csv_row["add_namespace"] == "pubchem_cid"
    assert csv_row["add_value"] == "702"


def test_unichem_adapter_marks_conflict_for_multiple_target_ids(monkeypatch):
    monkeypatch.setattr(
        "herg.sources.unichem_identifiers.fetch_identifier_enrichment_candidates",
        lambda target_namespace, limit=None, db_config=None: [
            {
                "compound_id": 2,
                "standard_inchikey": "",
                "chembl_id": "CHEMBL1089",
                "pubchem_cid": None,
                "unii": "",
                "preferred_name": "fixture",
            }
        ],
    )

    captured_payloads: list[dict] = []

    def fake_post_json(url, payload, config, label="UniChem"):
        captured_payloads.append(payload)
        return {
            "response": "Success",
            "compounds": [
                {
                    "sources": [
                        {"id": 22, "compoundId": "111"},
                        {"id": 22, "compoundId": "222"},
                    ]
                }
            ],
        }

    monkeypatch.setattr("herg.sources.unichem_identifiers.post_json", fake_post_json)

    adapter = UniChemIdentifierAdapter(
        http_config=HttpConfig(request_timeout_seconds=1, http_retries=0),
        target_namespace="pubchem_cid",
    )

    raw_rows = list(adapter.iter_raw_rows())
    row = adapter.enrich_batch(raw_rows)[0]

    assert captured_payloads == [{"compound": "CHEMBL1089", "type": "sourceID", "sourceID": 1}]
    assert row["harvest_status"] == "conflict"
    assert row["target_values"] == ["111", "222"]
    assert "multiple pubchem_cid mappings" in row["harvest_reason"]


def test_unichem_adapter_marks_error_for_lookup_failure(monkeypatch):
    monkeypatch.setattr(
        "herg.sources.unichem_identifiers.fetch_identifier_enrichment_candidates",
        lambda target_namespace, limit=None, db_config=None: [
            {
                "compound_id": 3,
                "standard_inchikey": "RYYVLZVUVIJVGH-UHFFFAOYSA-N",
                "chembl_id": "",
                "pubchem_cid": None,
                "unii": "",
                "preferred_name": "fixture",
            }
        ],
    )

    def fake_post_json(url, payload, config, label="UniChem"):
        raise RuntimeError("upstream timeout")

    monkeypatch.setattr("herg.sources.unichem_identifiers.post_json", fake_post_json)

    adapter = UniChemIdentifierAdapter(
        http_config=HttpConfig(request_timeout_seconds=1, http_retries=0),
        target_namespace="pubchem_cid",
    )

    raw_rows = list(adapter.iter_raw_rows())
    row = adapter.enrich_batch(raw_rows)[0]

    assert row["harvest_status"] == "error"
    assert "upstream timeout" in row["harvest_reason"]
