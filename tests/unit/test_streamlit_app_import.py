import importlib


def test_streamlit_app_imports_without_running_main():
    module = importlib.import_module("app")

    assert callable(module.main)
