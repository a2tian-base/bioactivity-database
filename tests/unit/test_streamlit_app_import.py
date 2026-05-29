import importlib

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
