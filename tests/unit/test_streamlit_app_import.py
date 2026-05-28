import importlib
from decimal import Decimal

import pandas as pd

from bioactivity.endpoints import EndpointConfig


def test_streamlit_app_imports_without_running_main():
    module = importlib.import_module("app")

    assert callable(module.main)


def _endpoint_config(endpoint_key: str = "herg_ic50") -> EndpointConfig:
    return EndpointConfig(
        endpoint_id=1,
        endpoint_key=endpoint_key,
        display_name="hERG IC50",
        spec={"measurement": {"type": "IC50", "value_kind": "concentration", "canonical_unit": "uM"}},
        source_configs={},
        spec_hash=f"{endpoint_key}-hash",
        active=True,
    )


def test_measurement_from_concentration_form_normalizes_allowed_units():
    module = importlib.import_module("app")

    measurement = module._measurement_from_concentration_form(
        source_record_key="manual:1",
        measurement_type="IC50",
        value=100.0,
        unit="nM",
        relation="=",
        canonical_unit="uM",
    )

    assert measurement.original_value == Decimal("100.0")
    assert measurement.original_unit == "nM"
    assert measurement.standard_value == Decimal("0.100")
    assert measurement.standard_unit == "uM"
    assert measurement.standard_relation == "="


def test_measurement_from_concentration_form_rejects_unconvertible_units():
    module = importlib.import_module("app")

    try:
        module._measurement_from_concentration_form(
            source_record_key="manual:1",
            measurement_type="IC50",
            value=100.0,
            unit="mg/mL",
            relation="=",
            canonical_unit="uM",
        )
    except ValueError as exc:
        assert "Cannot normalize concentration unit" in str(exc)
    else:
        raise AssertionError("Expected unsupported unit conversion to fail.")


def test_load_results_for_endpoint_falls_back_to_legacy_herg_rows(monkeypatch):
    module = importlib.import_module("app")

    class DummyConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    legacy_df = pd.DataFrame(
        [
            {
                "result_id": 10,
                "compound_id": 20,
                "source_record_id": 30,
                "ic50_value": Decimal("100"),
                "ic50_unit": "nM",
                "qualifier": "=",
                "ic50_um": Decimal("0.1"),
                "pic50": Decimal("7"),
                "pic50_qualifier": "=",
                "source_name": "legacy",
                "source_record_key": "legacy:1",
                "compound_label": "astemizole",
                "created_at": None,
                "updated_at": None,
            }
        ]
    )

    monkeypatch.setattr(module, "get_conn", lambda: DummyConnection())
    monkeypatch.setattr(module, "count_bioactivity_results", lambda conn, endpoint_id: 0)
    monkeypatch.setattr(module, "fetch_bioactivity_results", lambda conn, endpoint_id, limit: [])
    monkeypatch.setattr(module, "fetch_results_count", lambda: 1)
    monkeypatch.setattr(module, "fetch_results", lambda limit: legacy_df)

    total, results_df, data_source = module._load_results_for_endpoint(_endpoint_config(), 100)

    assert total == 1
    assert data_source == "legacy ic50_results"
    assert results_df.iloc[0]["measurement"] == "IC50 = 0.1 uM"
    assert results_df.iloc[0]["p_value_display"] == "pIC50 = 7"
