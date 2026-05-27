# Spec 006: Refactor ChEMBL adapter to accept endpoint source config

## Goal

Refactor the ChEMBL ingestion adapter so its hERG IC50 query parameters can come from a loaded endpoint's `source_configs["chembl"]` instead of being hard-coded or passed only as hERG-specific script arguments.

This spec should preserve current ChEMBL hERG IC50 behavior.

## Background

The current ChEMBL adapter already has configurable pieces such as:

```text
target_chembl_id
standard_type
relations / standard relations
page size
molecule batch size
```

For hERG IC50, those values should now be loaded from the saved endpoint row:

```json
{
  "target_chembl_id": "CHEMBL240",
  "standard_type": "IC50",
  "standard_relation__in": ["=", "<", ">"],
  "data_validity_comment__isnull": true
}
```

This spec moves ChEMBL endpoint variability into configuration, but it should not yet switch the whole pipeline to generic `bioactivity_results` writes.

## Non-goals

- Do not refactor the PubChem adapter.
- Do not change ChEMBL query semantics except to source parameters from config.
- Do not modify the main pipeline write path to `bioactivity_results`.
- Do not remove old ChEMBL script arguments or compatibility behavior.
- Do not remove `Ic50Input` or current staged record behavior.
- Do not make live ChEMBL calls in tests.
- Do not modify Streamlit UI.
- Do not add target catalog or ChEMBL catalog tables.

## Files likely to change

Likely files:

```text
app/herg/sources/chembl.py
app/herg/scripts/ingest_chembl_herg.py       # only for compatibility wiring if needed
app/bioactivity/endpoints.py                 # only if helper additions are needed
app/bioactivity/models.py
tests/fixtures/herg_ic50/...
tests/...
```

Follow the repository's actual script paths.

## Required behavior

After this spec is implemented:

```text
- ChEMBL adapter can still be constructed in the old hERG-specific way.
- ChEMBL adapter can also be constructed from an endpoint source config.
- For herg_ic50, the endpoint-config construction produces the same effective ChEMBL query parameters as the current hard-coded/default behavior.
- Existing hERG ChEMBL tests from Spec 001 still pass.
- ChEMBL row mapping for hERG IC50 remains unchanged.
- A generic MeasurementInput can be produced from a mapped ChEMBL hERG IC50 row, either directly or through a conversion helper.
```

## Database changes

No database changes.

## Python/API changes

### 1. Add endpoint-config construction path

Add a classmethod, factory, or initializer pattern equivalent to:

```python
ChemblAdapter.from_source_config(
    endpoint: EndpointConfig,
    source_config: dict[str, Any],
    **runtime_options,
)
```

or:

```python
ChemblAdapter(endpoint=endpoint, source_config=source_config, **runtime_options)
```

Use the least disruptive form for the existing codebase.

### 2. Preserve old construction path

Current scripts and tests that instantiate the adapter with explicit hERG parameters must continue to work.

If needed, implement old arguments as a wrapper that builds a source config internally.

### 3. Validate ChEMBL source config

Add lightweight validation for required ChEMBL keys:

```text
target_chembl_id
standard_type
```

Optional keys may include:

```text
standard_relation__in
data_validity_comment__isnull
page_size
molecule_batch_size
```

Validation should raise clear errors for missing required keys.

### 4. Preserve query construction

The effective ChEMBL API request for `herg_ic50` should remain equivalent to the existing request.

Do not introduce new filters such as assay confidence, relationship type, or organism unless already present in current behavior or the endpoint config already contains them.

### 5. Add generic measurement mapping hook

Add a narrow helper that converts a successfully mapped ChEMBL hERG IC50 row into `MeasurementInput`.

Acceptable approaches:

```text
- map_row continues returning the current staged record, and the staged record exposes measurement_input.
- map_row continues returning the current staged record, and a separate helper converts it to MeasurementInput.
- map_row returns a generic record only if the existing pipeline/tests are updated compatibly and behavior is preserved.
```

Prefer the first or second approach to minimize pipeline risk.

## Tests required

Tests must not call live ChEMBL.

Minimum tests:

```text
- Construct ChEMBL adapter using old explicit parameters; assert effective config matches existing behavior.
- Construct ChEMBL adapter from herg_ic50 endpoint source config; assert effective config matches old construction.
- Missing target_chembl_id raises clear validation error.
- Missing standard_type raises clear validation error.
- Fixture IC50 row maps the same as in Spec 001.
- Fixture IC50 row can be converted to MeasurementInput with measurement_type = IC50 and value_kind = concentration.
- Existing ChEMBL hERG tests still pass.
```

If current adapter internals make effective config hard to inspect, add a small test-only or public read-only property that exposes normalized config. Do not expose API credentials or raw response state.

## Validation commands

Preferred:

```bash
python -m pytest
```

## Acceptance criteria

- [ ] ChEMBL adapter supports endpoint source config construction.
- [ ] Old ChEMBL construction path remains compatible.
- [ ] hERG IC50 ChEMBL behavior is unchanged.
- [ ] ChEMBL source config validation exists.
- [ ] ChEMBL mapped row can produce a `MeasurementInput` or equivalent generic measurement.
- [ ] No PubChem adapter changes were made.
- [ ] No pipeline write-path changes were made.
- [ ] Tests use fixtures/mocks, not live ChEMBL calls.

## Notes for Codex

- Keep this refactor narrow. The purpose is config sourcing, not new ChEMBL scientific filtering.
- Do not rename the old hERG script in this spec.
- Keep source config keys aligned with `endpoints.source_configs["chembl"]` from Spec 002.
- If current code uses `relations` while endpoint config uses `standard_relation__in`, centralize the translation in one helper.
