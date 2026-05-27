import pytest

from herg.normalize import (
    build_identifier_inputs,
    build_name_inputs,
    clean_text,
    normalize_ic50_unit,
    normalize_qualifier,
    parse_bool,
    parse_positive_float,
    parse_positive_int,
)


def test_clean_text_handles_nulls():
    assert clean_text(None) == ""
    assert clean_text("  ") == ""
    assert clean_text("nan") == ""


@pytest.mark.parametrize(
    ("raw_unit", "expected"),
    [
        ("pM", "pM"),
        ("pm", "pM"),
        ("nM", "nM"),
        ("uM", "uM"),
        ("um", "uM"),
        ("\u00b5M", "uM"),
        ("\u03bcM", "uM"),
        ("mM", "mM"),
    ],
)
def test_normalize_ic50_unit(raw_unit, expected):
    assert normalize_ic50_unit(raw_unit) == expected


def test_normalize_ic50_unit_rejects_unknown_unit():
    with pytest.raises(ValueError):
        normalize_ic50_unit("kg")


@pytest.mark.parametrize("qualifier", ["=", "<", ">"])
def test_normalize_qualifier(qualifier):
    assert normalize_qualifier(qualifier) == qualifier


def test_normalize_qualifier_rejects_unknown_value():
    with pytest.raises(ValueError):
        normalize_qualifier("~")


def test_parse_positive_numbers():
    assert parse_positive_float("1.5") == 1.5
    with pytest.raises(ValueError):
        parse_positive_float("0")
    assert parse_positive_int("2") == 2
    with pytest.raises(ValueError):
        parse_positive_int("2.2")


def test_parse_bool():
    assert parse_bool("true") is True
    assert parse_bool("0") is False
    with pytest.raises(ValueError):
        parse_bool("maybe")


def test_build_identifier_inputs():
    identifiers = build_identifier_inputs({"chembl_id": "CHEMBL1", "chembl_id_dup": ""}, "chembl_id")
    assert len(identifiers) == 1
    assert identifiers[0].namespace == "chembl_id"
    assert identifiers[0].is_primary is True


def test_build_name_inputs():
    names = build_name_inputs(preferred_name="Test", aliases=["Test", "Alias"])
    assert names[0].name == "Test"
    assert names[0].is_preferred is True
    assert any(name.name == "Alias" for name in names)
