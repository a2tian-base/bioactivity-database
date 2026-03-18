from __future__ import annotations

from typing import Iterable

try:
    import pandas as pd
except ImportError:  # pragma: no cover - pandas is part of runtime deps
    pd = None

from .models import IdentifierInput, NameInput


ALLOWED_IC50_UNITS = {"pM", "nM", "uM", "mM"}
ALLOWED_QUALIFIERS = {"=", "<", ">"}


def clean_text(value: object) -> str:
    if value is None:
        return ""
    if pd is not None:
        try:
            if pd.isna(value):
                return ""
        except TypeError:
            pass
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def parse_positive_float(value: object) -> float:
    text = clean_text(value)
    if not text:
        raise ValueError("Expected positive number, got empty value.")
    try:
        parsed = float(text)
    except ValueError as exc:
        raise ValueError(f"Expected positive number, got '{text}'.") from exc
    if parsed <= 0:
        raise ValueError(f"Expected positive number, got '{text}'.")
    return parsed


def parse_positive_int(value: object) -> int:
    text = clean_text(value)
    if not text:
        raise ValueError("Expected positive integer, got empty value.")
    try:
        parsed = float(text)
    except ValueError as exc:
        raise ValueError(f"Expected positive integer, got '{text}'.") from exc
    if parsed <= 0 or not parsed.is_integer():
        raise ValueError(f"Expected positive integer, got '{text}'.")
    return int(parsed)


def parse_optional_positive_int(value: object) -> int | None:
    text = clean_text(value)
    if not text:
        return None
    return parse_positive_int(text)


def parse_bool(value: object) -> bool:
    text = clean_text(value).lower()
    if text in {"1", "true", "t", "yes", "y"}:
        return True
    if text in {"", "0", "false", "f", "no", "n"}:
        return False
    raise ValueError(f"Expected boolean value, got '{clean_text(value)}'.")


def normalize_ic50_unit(unit_value: object) -> str:
    raw = clean_text(unit_value).replace("\u00b5", "u").replace("\u03bc", "u").replace(" ", "")
    mapping = {
        "pm": "pM",
        "nm": "nM",
        "um": "uM",
        "mm": "mM",
    }
    unit = mapping.get(raw.lower(), raw)
    if unit not in ALLOWED_IC50_UNITS:
        raise ValueError(f"Invalid ic50_unit '{raw}'. Allowed: pM, nM, uM, mM.")
    return unit


def normalize_qualifier(qualifier_value: object) -> str:
    qualifier = clean_text(qualifier_value)
    if qualifier not in ALLOWED_QUALIFIERS:
        raise ValueError("Invalid qualifier. Allowed values are '=', '<', '>'.")
    return qualifier


def parse_pipe_or_comma_names(value: object) -> list[str]:
    text = clean_text(value)
    if not text:
        return []
    delimiter = "|" if "|" in text else ","
    names = [name.strip() for name in text.split(delimiter)]
    return [name for name in names if name]


def dedupe_casefolded(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = clean_text(value)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def build_identifier_inputs(
    identifiers: dict[str, object],
    primary_namespace: str | None = None,
) -> list[IdentifierInput]:
    result: list[IdentifierInput] = []
    seen: set[tuple[str, str]] = set()
    primary = clean_text(primary_namespace).lower() if primary_namespace else None

    for namespace, value in identifiers.items():
        cleaned = clean_text(value)
        if not cleaned:
            continue
        ns = clean_text(namespace).lower()
        if not ns:
            continue
        key = (ns, cleaned.casefold())
        if key in seen:
            continue
        seen.add(key)
        result.append(IdentifierInput(namespace=ns, value=cleaned, is_primary=primary == ns))

    return result


def build_name_inputs(
    preferred_name: str = "",
    aliases: Iterable[str] | str | None = None,
) -> list[NameInput]:
    result: list[NameInput] = []
    preferred_clean = clean_text(preferred_name)
    if preferred_clean:
        result.append(NameInput(name=preferred_clean, name_type="preferred", is_preferred=True))

    if aliases is None:
        alias_list: list[str] = []
    elif isinstance(aliases, str):
        alias_list = parse_pipe_or_comma_names(aliases)
    else:
        alias_list = [clean_text(alias) for alias in aliases]

    alias_list = dedupe_casefolded(alias_list)
    if preferred_clean:
        alias_list = [name for name in alias_list if name.casefold() != preferred_clean.casefold()]

    for name in alias_list:
        result.append(NameInput(name=name, name_type="alias", is_preferred=False))

    return result
