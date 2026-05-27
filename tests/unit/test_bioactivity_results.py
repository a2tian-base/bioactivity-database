from decimal import Decimal

from bioactivity.endpoints import EndpointConfig
from bioactivity.results import format_bioactivity_result_row, manual_entry_schema


def _endpoint_config(
    *,
    endpoint_key: str = "herg_ic50",
    display_name: str = "hERG IC50",
    measurement_type: str = "IC50",
    value_kind: str = "concentration",
) -> EndpointConfig:
    return EndpointConfig(
        endpoint_id=1,
        endpoint_key=endpoint_key,
        display_name=display_name,
        spec={
            "measurement": {
                "type": measurement_type,
                "value_kind": value_kind,
                "canonical_unit": "uM",
            },
            "normalization": {
                "allowed_units": ["pM", "nM", "uM", "mM"],
                "allowed_relations": ["=", "<", ">"],
            },
        },
        source_configs={},
        spec_hash=f"{endpoint_key}-hash",
        active=True,
    )


def test_format_bioactivity_result_row_handles_concentration_with_p_value():
    formatted = format_bioactivity_result_row(
        {
            "result_id": 1,
            "compound_id": 10,
            "compound_label": "astemizole",
            "source_name": "chembl",
            "source_record_key": "activity:1",
            "measurement_type": "IC50",
            "value_kind": "concentration",
            "standard_value": Decimal("0.1000"),
            "standard_unit": "uM",
            "standard_relation": "=",
            "p_value": Decimal("7.0000"),
            "p_value_relation": "=",
        }
    )

    assert formatted["measurement"] == "IC50 = 0.1 uM"
    assert formatted["p_value_display"] == "pIC50 = 7"
    assert formatted["compound"] == "astemizole"


def test_format_bioactivity_result_row_handles_categorical_without_p_value():
    formatted = format_bioactivity_result_row(
        {
            "measurement_type": "Ames",
            "value_kind": "categorical",
            "value_text": "positive",
            "source_name": "fixture",
        }
    )

    assert formatted["measurement"] == "Ames: positive"
    assert formatted["p_value_display"] == ""
    assert formatted["value_text"] == "positive"


def test_manual_entry_schema_returns_concentration_fields_for_herg_ic50():
    schema = manual_entry_schema(_endpoint_config())

    assert schema.supported is True
    assert schema.measurement_type == "IC50"
    assert schema.value_kind == "concentration"
    assert schema.canonical_unit == "uM"
    assert schema.allowed_units == ["pM", "nM", "uM", "mM"]
    assert schema.allowed_relations == ["=", "<", ">"]


def test_manual_entry_schema_returns_clear_unsupported_state():
    schema = manual_entry_schema(
        _endpoint_config(
            endpoint_key="ames_outcome",
            display_name="Ames outcome",
            measurement_type="Ames",
            value_kind="categorical",
        )
    )

    assert schema.supported is False
    assert schema.value_kind == "categorical"
    assert "concentration endpoints only" in schema.message
    assert "categorical" in schema.message
