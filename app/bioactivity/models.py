from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any


ALLOWED_VALUE_KINDS = frozenset({"concentration", "percent", "numeric", "categorical", "text"})


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Numeric measurement values must not be boolean.")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid numeric measurement value '{value}'.") from exc


def _dict_copy(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping.")
    return dict(value)


@dataclass(frozen=True)
class MeasurementInput:
    result_key: str
    measurement_type: str
    value_kind: str
    original_value: Decimal | None = None
    original_unit: str | None = None
    original_relation: str | None = None
    standard_value: Decimal | None = None
    standard_unit: str | None = None
    standard_relation: str | None = None
    p_value: Decimal | None = None
    p_value_relation: str | None = None
    value_text: str | None = None
    assay_context: dict[str, Any] = field(default_factory=dict)
    quality_flags: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        result_key = _clean_text(self.result_key)
        measurement_type = _clean_text(self.measurement_type)
        value_kind = _clean_text(self.value_kind)
        if not result_key:
            raise ValueError("result_key is required.")
        if not measurement_type:
            raise ValueError("measurement_type is required.")
        if value_kind not in ALLOWED_VALUE_KINDS:
            allowed = ", ".join(sorted(ALLOWED_VALUE_KINDS))
            raise ValueError(f"Invalid value_kind '{value_kind}'. Allowed: {allowed}.")

        object.__setattr__(self, "result_key", result_key)
        object.__setattr__(self, "measurement_type", measurement_type)
        object.__setattr__(self, "value_kind", value_kind)
        object.__setattr__(self, "original_value", _decimal_or_none(self.original_value))
        object.__setattr__(self, "standard_value", _decimal_or_none(self.standard_value))
        object.__setattr__(self, "p_value", _decimal_or_none(self.p_value))
        object.__setattr__(self, "assay_context", _dict_copy(self.assay_context, "assay_context"))
        object.__setattr__(self, "quality_flags", _dict_copy(self.quality_flags, "quality_flags"))

        if value_kind == "concentration":
            self._validate_concentration()
        elif value_kind == "percent":
            self._validate_percent()
        elif value_kind == "categorical":
            self._validate_categorical()
        elif value_kind == "text":
            self._validate_text()

    def _validate_concentration(self) -> None:
        if self.standard_value is not None and not _clean_text(self.standard_unit):
            raise ValueError("standard_unit is required when standard_value is present.")

    def _validate_percent(self) -> None:
        if self.p_value is not None:
            raise ValueError("p_value is not valid for percent measurements.")
        if self.standard_value is not None and self.standard_unit != "%":
            raise ValueError("standard_unit must be '%' for percent measurements with standard_value.")

    def _validate_categorical(self) -> None:
        if not _clean_text(self.value_text):
            raise ValueError("value_text is required for categorical measurements.")
        if self.p_value is not None:
            raise ValueError("p_value is not valid for categorical measurements.")

    def _validate_text(self) -> None:
        if not _clean_text(self.value_text):
            raise ValueError("value_text is required for text measurements.")
        if self.p_value is not None:
            raise ValueError("p_value is not valid for text measurements.")


def measurement_from_ic50(
    *,
    result_key: str,
    ic50_value: Decimal | int | float | str,
    ic50_unit: str,
    qualifier: str | None,
    ic50_um: Decimal | int | float | str | None = None,
    pic50: Decimal | int | float | str | None = None,
    pic50_qualifier: str | None = None,
    assay_context: dict[str, Any] | None = None,
    quality_flags: dict[str, Any] | None = None,
) -> MeasurementInput:
    standard_unit = "uM" if ic50_um is not None else None
    return MeasurementInput(
        result_key=result_key,
        measurement_type="IC50",
        value_kind="concentration",
        original_value=_decimal_or_none(ic50_value),
        original_unit=ic50_unit,
        original_relation=qualifier,
        standard_value=_decimal_or_none(ic50_um),
        standard_unit=standard_unit,
        standard_relation=qualifier if ic50_um is not None else None,
        p_value=_decimal_or_none(pic50),
        p_value_relation=pic50_qualifier,
        assay_context=assay_context or {},
        quality_flags=quality_flags or {},
    )
