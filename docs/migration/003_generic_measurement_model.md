# Spec 003: Add generic measurement models

## Goal

Introduce application-layer models for generic bioactivity measurements without changing production ingestion behavior.

This spec creates the typed object that later specs will write into `bioactivity_results`. It should preserve current hERG IC50 behavior and should not yet make ChEMBL, PubChem, or the pipeline write generic results.

## Background

The current code is organized around IC50-specific inputs and validation. The migration target uses a generic `bioactivity_results` table that can store concentration, percent, numeric, categorical, and text endpoints.

This spec introduces the generic measurement representation in Python so later specs can convert source rows into a stable object before database writes.

The generic result shape is:

```text
measurement_type
value_kind
original_value, original_unit, original_relation
standard_value, standard_unit, standard_relation
p_value, p_value_relation
value_text
assay_context
quality_flags
result_key
```

## Non-goals

- Do not change database schema.
- Do not modify ChEMBL or PubChem adapter behavior.
- Do not modify the ingestion pipeline write path.
- Do not write to `bioactivity_results` yet.
- Do not remove or rename existing IC50-specific models.
- Do not remove or change `ic50_results`.
- Do not modify Streamlit UI.
- Do not add endpoint discovery, target catalog, assay catalog, or measurement ontology tables.
- Do not make tests call live external services.

## Files likely to change

Codex should inspect current package structure first. Likely additions or changes include:

```text
app/herg/models.py                  # if current models live here
app/bioactivity/models.py           # preferred if a generic package already exists or is easy to add
app/bioactivity/__init__.py
tests/...
```

Use the repository's existing layout if it already has a better location for shared models.

## Required behavior

After this spec is implemented:

```text
- A generic MeasurementInput model exists.
- MeasurementInput supports concentration, percent, numeric, categorical, and text value kinds.
- MeasurementInput can represent current hERG IC50 rows without losing information.
- MeasurementInput validation rejects invalid value kinds and internally inconsistent values.
- Existing hERG IC50 models and code paths continue to work.
- Tests cover model construction and validation.
```

## Database changes

No database changes.

## Python/API changes

### 1. Add `MeasurementInput`

Add a generic Python model equivalent to:

```python
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
```

If the repository already uses Pydantic or another validation layer, follow that convention instead of forcing a dataclass. The behavior matters more than the exact implementation.

### 2. Validate `value_kind`

Allowed values:

```text
concentration
percent
numeric
categorical
text
```

Reject any other value.

### 3. Validate core consistency

Implement lightweight validation. Do not overbuild.

Recommended rules:

```text
All MeasurementInput objects:
  result_key must be non-empty.
  measurement_type must be non-empty.
  assay_context must be a mapping/dict.
  quality_flags must be a mapping/dict.

concentration:
  standard_value should be present when the row has a numeric result.
  standard_unit should be present when standard_value is present.
  p_value is allowed but not required.

percent:
  p_value must be null.
  standard_unit should be "%" when standard_value is present.

numeric:
  p_value should normally be null unless explicitly justified by caller.

categorical:
  value_text must be present.
  standard_value should normally be null.
  p_value must be null.

text:
  value_text must be present.
  p_value must be null.
```

Do not reject rows solely because `original_value` is missing. Some source rows may only have a standardized value, and some categorical rows may only have text.

### 4. Add conversion helper for current IC50 behavior

Add a small helper so current IC50 data can be represented as a `MeasurementInput`.

Example conceptual helper:

```python
def measurement_from_ic50(
    *,
    result_key: str,
    ic50_value: Decimal,
    ic50_unit: str,
    qualifier: str | None,
    ic50_um: Decimal | None = None,
    pic50: Decimal | None = None,
    pic50_qualifier: str | None = None,
    assay_context: dict[str, Any] | None = None,
    quality_flags: dict[str, Any] | None = None,
) -> MeasurementInput:
    ...
```

The exact signature may vary based on existing code, but it must not change current IC50 normalization semantics.

### 5. Keep IC50 compatibility

If an existing `Ic50Input` or equivalent model exists, do not remove it. Later specs will migrate usage gradually.

It is acceptable to add a conversion function between `Ic50Input` and `MeasurementInput` if that simplifies later work.

## Tests required

Add deterministic unit tests for `MeasurementInput`.

Minimum tests:

```text
- concentration MeasurementInput accepts an IC50-like record with p_value.
- percent MeasurementInput accepts standard_value with standard_unit = "%".
- categorical MeasurementInput requires value_text.
- text MeasurementInput requires value_text.
- invalid value_kind is rejected.
- empty result_key is rejected.
- empty measurement_type is rejected.
- p_value is rejected for categorical and percent records.
- assay_context and quality_flags default to empty dictionaries.
- IC50 conversion helper preserves measurement_type = IC50 and value_kind = concentration.
```

Do not require a database for these tests.

## Validation commands

Preferred:

```bash
python -m pytest
```

If the repository uses a different test command, use that command and document it in the PR summary.

## Acceptance criteria

- [ ] Generic `MeasurementInput` or equivalent model exists.
- [ ] The model can represent hERG IC50 concentration data.
- [ ] The model can represent non-concentration data kinds.
- [ ] Invalid value kinds and inconsistent categorical/percent records are rejected.
- [ ] Existing IC50-specific code remains import-compatible.
- [ ] No database schema changes were made.
- [ ] No adapter or pipeline behavior was changed.
- [ ] Unit tests were added and pass.

## Notes for Codex

- Prefer additive code over rewriting existing hERG modules.
- If adding `app/bioactivity/`, keep it small and focused.
- Use `Decimal` for numeric values if the current code already uses precise decimal handling for assay values.
- Do not introduce a new dependency for validation unless the repository already uses it.
- Keep validation strict enough to catch obvious misuse but flexible enough for heterogeneous source rows.
