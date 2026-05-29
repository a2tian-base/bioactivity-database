from decimal import Decimal

import pytest

from bioactivity.models import MeasurementInput, measurement_from_ic50
from herg.models import Ic50Input


def test_concentration_measurement_accepts_ic50_like_record_with_p_value():
    measurement = MeasurementInput(
        result_key="activity:123",
        measurement_type="IC50",
        value_kind="concentration",
        original_value=Decimal("50"),
        original_unit="nM",
        original_relation="<",
        standard_value=Decimal("0.05"),
        standard_unit="uM",
        standard_relation="<",
        p_value=Decimal("7.3010"),
        p_value_relation=">",
    )

    assert measurement.measurement_type == "IC50"
    assert measurement.value_kind == "concentration"
    assert measurement.original_value == Decimal("50")
    assert measurement.standard_value == Decimal("0.05")
    assert measurement.p_value == Decimal("7.3010")
    assert measurement.p_value_relation == ">"


def test_percent_measurement_accepts_percent_standard_unit():
    measurement = MeasurementInput(
        result_key="assay:1",
        measurement_type="inhibition",
        value_kind="percent",
        standard_value=Decimal("72"),
        standard_unit="%",
    )

    assert measurement.standard_value == Decimal("72")
    assert measurement.standard_unit == "%"


def test_numeric_measurement_accepts_numeric_endpoint():
    measurement = MeasurementInput(
        result_key="property:1",
        measurement_type="solubility",
        value_kind="numeric",
        standard_value="12.5",
        standard_unit="ug/mL",
    )

    assert measurement.standard_value == Decimal("12.5")


def test_categorical_measurement_requires_value_text():
    with pytest.raises(ValueError, match="value_text is required"):
        MeasurementInput(
            result_key="assay:outcome",
            measurement_type="activity_outcome",
            value_kind="categorical",
        )

    measurement = MeasurementInput(
        result_key="assay:outcome",
        measurement_type="activity_outcome",
        value_kind="categorical",
        value_text="active",
    )
    assert measurement.value_text == "active"


def test_text_measurement_requires_value_text():
    with pytest.raises(ValueError, match="value_text is required"):
        MeasurementInput(
            result_key="note:1",
            measurement_type="comment",
            value_kind="text",
        )

    measurement = MeasurementInput(
        result_key="note:1",
        measurement_type="comment",
        value_kind="text",
        value_text="observed precipitate",
    )
    assert measurement.value_text == "observed precipitate"


def test_invalid_value_kind_is_rejected():
    with pytest.raises(ValueError, match="Invalid value_kind"):
        MeasurementInput(
            result_key="result:1",
            measurement_type="IC50",
            value_kind="boolean",
        )


def test_empty_result_key_and_measurement_type_are_rejected():
    with pytest.raises(ValueError, match="result_key is required"):
        MeasurementInput(
            result_key=" ",
            measurement_type="IC50",
            value_kind="concentration",
        )
    with pytest.raises(ValueError, match="measurement_type is required"):
        MeasurementInput(
            result_key="result:1",
            measurement_type=" ",
            value_kind="concentration",
        )


@pytest.mark.parametrize("value_kind", ["percent", "categorical", "text"])
def test_p_value_is_rejected_for_non_potency_value_kinds(value_kind):
    kwargs = {
        "result_key": f"{value_kind}:1",
        "measurement_type": value_kind,
        "value_kind": value_kind,
        "p_value": Decimal("6.5"),
    }
    if value_kind in {"categorical", "text"}:
        kwargs["value_text"] = "active"

    with pytest.raises(ValueError, match="p_value is not valid"):
        MeasurementInput(**kwargs)


def test_assay_context_and_quality_flags_default_to_empty_dictionaries():
    measurement = MeasurementInput(
        result_key="result:1",
        measurement_type="IC50",
        value_kind="concentration",
    )

    assert measurement.assay_context == {}
    assert measurement.quality_flags == {}


def test_assay_context_and_quality_flags_must_be_mappings():
    with pytest.raises(ValueError, match="assay_context must be a mapping"):
        MeasurementInput(
            result_key="result:1",
            measurement_type="IC50",
            value_kind="concentration",
            assay_context=[],
        )
    with pytest.raises(ValueError, match="quality_flags must be a mapping"):
        MeasurementInput(
            result_key="result:1",
            measurement_type="IC50",
            value_kind="concentration",
            quality_flags=[],
        )


def test_ic50_conversion_helper_preserves_current_ic50_fields():
    measurement = measurement_from_ic50(
        result_key="activity:123",
        ic50_value=Decimal("50"),
        ic50_unit="nM",
        qualifier="<",
        ic50_um=Decimal("0.05"),
        pic50=Decimal("7.3010"),
        pic50_qualifier=">",
        assay_context={"assay_chembl_id": "CHEMBL123"},
        quality_flags={"source": "fixture"},
    )

    assert measurement.result_key == "activity:123"
    assert measurement.measurement_type == "IC50"
    assert measurement.value_kind == "concentration"
    assert measurement.original_value == Decimal("50")
    assert measurement.original_unit == "nM"
    assert measurement.original_relation == "<"
    assert measurement.standard_value == Decimal("0.05")
    assert measurement.standard_unit == "uM"
    assert measurement.standard_relation == "<"
    assert measurement.p_value == Decimal("7.3010")
    assert measurement.p_value_relation == ">"
    assert measurement.assay_context == {"assay_chembl_id": "CHEMBL123"}
    assert measurement.quality_flags == {"source": "fixture"}


def test_existing_ic50_model_remains_import_compatible():
    measurement = Ic50Input(ic50_value=50.0, ic50_unit="nM", qualifier="<", endpoint="IC50")

    assert measurement.ic50_value == 50.0
    assert measurement.ic50_unit == "nM"
    assert measurement.qualifier == "<"
    assert measurement.endpoint == "IC50"
