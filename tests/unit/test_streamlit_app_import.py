import importlib

from bioactivity.preview import PreviewExample, PreviewResult


def test_streamlit_app_imports_without_running_main():
    module = importlib.import_module("app")

    assert callable(module.main)


def test_preview_result_display_data_handles_fake_preview_result():
    module = importlib.import_module("app")
    result = PreviewResult(
        endpoint_key="herg_ic50",
        source_name="chembl",
        query_config={"target_chembl_id": "CHEMBL240"},
        raw_rows_examined=2,
        accepted_count=1,
        skipped_count=1,
        error_count=0,
        accepted_examples=[
            PreviewExample(
                external_key="activity:1",
                source_record_key="activity:1",
                measurement={"measurement_type": "IC50", "value_kind": "concentration"},
            )
        ],
        skipped_examples=[
            PreviewExample(
                external_key="activity:bad",
                reason="Missing molecule metadata.",
            )
        ],
        warnings=["fixture warning"],
    )

    display = module.preview_result_display_data(result)

    assert display["summary"] == {
        "raw_rows_examined": 2,
        "accepted": 1,
        "skipped": 1,
        "errors": 0,
    }
    assert display["query_config"] == {"target_chembl_id": "CHEMBL240"}
    assert display["accepted"].iloc[0]["external_key"] == "activity:1"
    assert display["skipped"].iloc[0]["reason"] == "Missing molecule metadata."
    assert display["warnings"] == ["fixture warning"]
