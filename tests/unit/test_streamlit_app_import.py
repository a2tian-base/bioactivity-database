import importlib
from decimal import Decimal

import pandas as pd

from bioactivity.endpoints import EndpointConfig
from bioactivity.preview import PreviewExample, PreviewResult


class _StreamlitContext:
    def __init__(self, fake_st, name):
        self.fake_st = fake_st
        self.name = name

    def __enter__(self):
        self.fake_st.context.append(self.name)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.fake_st.context.pop()
        return False


class _FakeStreamlit:
    def __init__(self):
        self.context = []
        self.expanders = []
        self.number_inputs = []
        self.checkboxes = []
        self.buttons = []

    def subheader(self, *args, **kwargs):
        pass

    def caption(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def markdown(self, *args, **kwargs):
        pass

    def selectbox(self, label, *, options, **kwargs):
        return options[0]

    def number_input(self, label, **kwargs):
        self.number_inputs.append(
            {
                "label": label,
                "value": kwargs.get("value"),
                "context": tuple(self.context),
            }
        )
        return kwargs.get("value")

    def checkbox(self, label, *, value=False, **kwargs):
        self.checkboxes.append({"label": label, "value": value, "context": tuple(self.context)})
        return value

    def button(self, label, **kwargs):
        self.buttons.append({"label": label, "context": tuple(self.context)})
        return False

    def expander(self, label, **kwargs):
        self.expanders.append(label)
        return _StreamlitContext(self, f"expander:{label}")


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


def test_ingest_tab_keeps_operational_tuning_in_advanced_configuration(monkeypatch):
    module = importlib.import_module("app")
    fake_st = _FakeStreamlit()
    endpoint = EndpointConfig(
        endpoint_id=1,
        endpoint_key="herg_ic50",
        display_name="hERG IC50",
        spec={"measurement": {"type": "IC50", "value_kind": "concentration"}},
        source_configs={"chembl": {"target_chembl_id": "CHEMBL240"}},
        spec_hash="fixture",
        active=True,
    )

    monkeypatch.setattr(module, "st", fake_st)

    module.render_ingest_tab(endpoint)

    assert fake_st.expanders == ["Advanced configuration"]
    primary_inputs = {control["label"] for control in fake_st.number_inputs if not control["context"]}
    advanced_inputs = {
        control["label"]
        for control in fake_st.number_inputs
        if control["context"] == ("expander:Advanced configuration",)
    }
    primary_checkboxes = {control["label"] for control in fake_st.checkboxes if not control["context"]}
    advanced_checkboxes = {
        control["label"]
        for control in fake_st.checkboxes
        if control["context"] == ("expander:Advanced configuration",)
    }

    assert primary_inputs == {"Max records"}
    assert primary_checkboxes == {"Dry run"}
    assert advanced_inputs == {"Preview limit", "Request timeout seconds", "HTTP retries", "Commit every"}
    assert advanced_checkboxes == {"Fail fast"}


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
