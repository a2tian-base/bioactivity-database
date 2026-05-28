import importlib
import re

import pandas as pd

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


def test_histogram_chart_data_formats_x_axis_labels_to_two_decimals():
    module = importlib.import_module("app")
    histogram_counts = module.build_histogram_counts(pd.Series([1.0, 1.5, 2.0]), bins=2)

    chart_data = module.histogram_chart_data(histogram_counts)

    assert list(chart_data.columns) == ["count"]
    assert chart_data.index.name == "bin"
    assert chart_data.index.is_unique
    assert all(
        re.fullmatch(r"-?\d+\.\d{2} to -?\d+\.\d{2}", label) for label in chart_data.index
    )


def test_histogram_chart_data_keeps_narrow_bin_labels_unique():
    module = importlib.import_module("app")
    values = pd.Series([7.0 + (step * 0.001) for step in range(31)])
    histogram_counts = module.build_histogram_counts(values, bins=30)

    rounded_starts = histogram_counts["bin_start"].map(lambda value: f"{value:.2f}")
    chart_data = module.histogram_chart_data(histogram_counts)

    assert not rounded_starts.is_unique
    assert chart_data.index.is_unique
    assert len(chart_data) == len(histogram_counts)
