from __future__ import annotations

import importlib
from collections.abc import Iterator
from contextlib import contextmanager

from bioactivity.endpoint_search import EndpointSearchResult, SavedEndpointSearchResult
from bioactivity.endpoints import EndpointConfig
from bioactivity.source_discovery import EndpointCandidate, SourceAvailability


class _StreamlitContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeStreamlit:
    def __init__(
        self,
        *,
        clicked_keys: set[str] | None = None,
        text_inputs: dict[str, str] | None = None,
        selectboxes: dict[str, str] | None = None,
        multiselects: dict[str, list[str]] | None = None,
    ):
        self.session_state: dict[str, object] = {}
        self.clicked_keys = clicked_keys or set()
        self.text_inputs = text_inputs or {}
        self.selectboxes = selectboxes or {}
        self.multiselects = multiselects or {}
        self.markdowns: list[str] = []
        self.captions: list[str] = []
        self.writes: list[str] = []
        self.infos: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.successes: list[str] = []
        self.buttons: list[dict[str, object]] = []

    def subheader(self, text, *args, **kwargs):
        self.markdowns.append(str(text))

    def markdown(self, text, *args, **kwargs):
        self.markdowns.append(str(text))

    def caption(self, text, *args, **kwargs):
        self.captions.append(str(text))

    def write(self, text, *args, **kwargs):
        self.writes.append(str(text))

    def info(self, text, *args, **kwargs):
        self.infos.append(str(text))

    def warning(self, text, *args, **kwargs):
        self.warnings.append(str(text))

    def error(self, text, *args, **kwargs):
        self.errors.append(str(text))

    def success(self, text, *args, **kwargs):
        self.successes.append(str(text))

    def text_input(self, label, *, value="", key=None, **kwargs):
        lookup_key = key or label
        return self.text_inputs.get(lookup_key, value)

    def selectbox(self, label, *, options, index=0, key=None, **kwargs):
        lookup_key = key or label
        return self.selectboxes.get(lookup_key, options[index])

    def multiselect(self, label, *, options, default=None, key=None, **kwargs):
        lookup_key = key or label
        return self.multiselects.get(lookup_key, list(default or []))

    def button(self, label, *, key=None, **kwargs):
        self.buttons.append({"label": label, "key": key})
        return bool(key and key in self.clicked_keys)

    def spinner(self, text, *args, **kwargs):
        return _StreamlitContext()

    def expander(self, label, **kwargs):
        return _StreamlitContext()

    def divider(self):
        pass


class _FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _saved_herg_endpoint() -> SavedEndpointSearchResult:
    return SavedEndpointSearchResult(
        endpoint_id=1,
        endpoint_key="herg_ic50",
        display_name="hERG IC50",
        target_name="hERG",
        gene_symbol="KCNH2",
        organism="Homo sapiens",
        measurement_type="IC50",
        value_kind="concentration",
        source_names=("chembl", "pubchem"),
        active=True,
        score=150.0,
    )


def _egfr_candidate() -> EndpointCandidate:
    return EndpointCandidate(
        candidate_key="chembl_chembl203_ic50",
        display_name="EGFR IC50",
        template_key="concentration_potency",
        spec={
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
        source_configs={
            "chembl": {
                "target_chembl_id": "CHEMBL203",
                "standard_type": "IC50",
            }
        },
        source_availability=(
            SourceAvailability(
                source_name="chembl",
                source_target_id="CHEMBL203",
                measurement_type="IC50",
                approximate_count=42,
            ),
        ),
        warnings=("Review ChEMBL target assignment.",),
        score=98.5,
    )


def _endpoint_config(endpoint_key: str = "egfr_ic50") -> EndpointConfig:
    return EndpointConfig(
        endpoint_id=2,
        endpoint_key=endpoint_key,
        display_name="EGFR IC50",
        spec={
            "target": {
                "preferred_name": "Epidermal growth factor receptor",
                "gene_symbol": "EGFR",
                "organism": "Homo sapiens",
            },
            "measurement": {"type": "IC50", "value_kind": "concentration"},
        },
        source_configs={"chembl": {"target_chembl_id": "CHEMBL203"}},
        spec_hash="fixture",
        active=True,
    )


@contextmanager
def _fake_get_conn() -> Iterator[_FakeConnection]:
    yield _FakeConnection()


def test_streamlit_app_imports_without_network_access():
    module = importlib.import_module("app")

    assert callable(module.render_find_endpoint_tab)


def test_search_button_calls_search_service_under_mock(monkeypatch):
    module = importlib.import_module("app")
    result = EndpointSearchResult(saved_endpoints=(_saved_herg_endpoint(),), candidates=(), warnings=())
    calls: list[dict[str, object]] = []
    fake_st = _FakeStreamlit(
        clicked_keys={"endpoint_search_button"},
        text_inputs={
            "endpoint_search_query": "hERG IC50",
            "endpoint_search_organism": "Homo sapiens",
        },
        selectboxes={"endpoint_search_measurement_type": "Auto"},
        multiselects={"endpoint_search_sources": ["ChEMBL"]},
    )

    def fake_search_endpoints(conn, query, **kwargs):
        calls.append({"conn": conn, "query": query, **kwargs})
        return result

    monkeypatch.setattr(module, "st", fake_st)
    monkeypatch.setattr(module, "get_conn", _fake_get_conn)
    monkeypatch.setattr(module, "search_endpoints", fake_search_endpoints)

    module.render_find_endpoint_tab()

    assert calls == [
        {
            "conn": calls[0]["conn"],
            "query": "hERG IC50",
            "sources": ("chembl",),
            "organism": "Homo sapiens",
            "measurement_type": None,
        }
    ]
    assert fake_st.session_state[module.ENDPOINT_SEARCH_RESULT_KEY] == result


def test_saved_endpoint_results_render_from_fake_result(monkeypatch):
    module = importlib.import_module("app")
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(module, "st", fake_st)
    result = EndpointSearchResult(saved_endpoints=(_saved_herg_endpoint(),), candidates=(), warnings=())

    module.render_endpoint_search_results(result)

    assert any("hERG IC50" in value for value in fake_st.markdowns)
    assert any("Endpoint key: `herg_ic50`" in value for value in fake_st.captions)
    assert any("KCNH2" in value and "Homo sapiens" in value for value in fake_st.writes)
    assert any("IC50" in value and "concentration" in value for value in fake_st.writes)
    assert any(button["key"] == "select_saved_endpoint_herg_ic50" for button in fake_st.buttons)


def test_candidate_results_render_from_fake_result(monkeypatch):
    module = importlib.import_module("app")
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(module, "st", fake_st)
    candidate = _egfr_candidate()
    result = EndpointSearchResult(saved_endpoints=(), candidates=(candidate,), warnings=())

    module.render_endpoint_search_results(result)

    assert any("EGFR IC50" in value for value in fake_st.markdowns)
    assert any("Candidate key: `chembl_chembl203_ic50`" in value for value in fake_st.captions)
    assert any("EGFR" in value and "Homo sapiens" in value for value in fake_st.writes)
    assert any("chembl: IC50 on CHEMBL203" in value for value in fake_st.writes)
    assert any(button["key"] == "save_candidate_chembl_chembl203_ic50" for button in fake_st.buttons)


def test_save_endpoint_action_calls_save_endpoint_candidate_under_mock(monkeypatch):
    module = importlib.import_module("app")
    candidate = _egfr_candidate()
    fake_st = _FakeStreamlit(clicked_keys={"save_candidate_chembl_chembl203_ic50"})
    saved_endpoint = _endpoint_config()
    calls: list[EndpointCandidate] = []

    def fake_save_endpoint_candidate(conn, candidate_arg):
        calls.append(candidate_arg)
        return saved_endpoint

    monkeypatch.setattr(module, "st", fake_st)
    monkeypatch.setattr(module, "get_conn", _fake_get_conn)
    monkeypatch.setattr(module, "save_endpoint_candidate", fake_save_endpoint_candidate)
    monkeypatch.setattr(module, "load_endpoint", lambda conn, endpoint_key: saved_endpoint)

    module.render_endpoint_candidate_search_result(candidate)

    assert calls == [candidate]
    assert any("Saved endpoint" in message and "EGFR IC50" in message for message in fake_st.successes)


def test_external_search_not_called_during_initial_render(monkeypatch):
    module = importlib.import_module("app")
    fake_st = _FakeStreamlit()

    def fail_search(*args, **kwargs):
        raise AssertionError("search_endpoints should only run when Search is clicked")

    monkeypatch.setattr(module, "st", fake_st)
    monkeypatch.setattr(module, "search_endpoints", fail_search)

    module.render_find_endpoint_tab()

    assert fake_st.errors == []
    assert module.ENDPOINT_SEARCH_RESULT_KEY not in fake_st.session_state
