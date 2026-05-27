# Spec 007: Refactor PubChem adapter to accept endpoint source config

## Goal

Refactor the PubChem ingestion adapter so its hERG IC50 query and row-filtering parameters can come from a loaded endpoint's `source_configs["pubchem"]` instead of being hard-coded or passed only as hERG-specific script arguments.

This spec should preserve current PubChem hERG IC50 behavior.

## Background

The current PubChem adapter uses hERG-specific parameters such as:

```text
target_gene_symbol
target_gene_id
activity_name_regex
```

For hERG IC50, these values should now come from the saved endpoint row:

```json
{
  "target_gene_symbol": "KCNH2",
  "target_gene_id": "3757",
  "activity_name_regex": "(?i)\\bIC50\\b"
}
```

This spec moves PubChem endpoint variability into configuration. It does not yet change the main pipeline write path.

## Non-goals

- Do not refactor the ChEMBL adapter.
- Do not change PubChem query/filter semantics except to source parameters from config.
- Do not modify the main pipeline write path to `bioactivity_results`.
- Do not remove old PubChem script arguments or compatibility behavior.
- Do not remove `Ic50Input` or current staged record behavior.
- Do not make live PubChem calls in tests.
- Do not modify Streamlit UI.
- Do not add PubChem target/assay catalog tables.

## Files likely to change

Likely files:

```text
app/herg/sources/pubchem.py
app/herg/scripts/ingest_pubchem_herg.py      # only for compatibility wiring if needed
app/bioactivity/endpoints.py                 # only if helper additions are needed
app/bioactivity/models.py
tests/fixtures/herg_ic50/...
tests/...
```

Follow the repository's actual script paths.

## Required behavior

After this spec is implemented:

```text
- PubChem adapter can still be constructed in the old hERG-specific way.
- PubChem adapter can also be constructed from an endpoint source config.
- For herg_ic50, endpoint-config construction produces the same effective PubChem filters as the current default behavior.
- Existing hERG PubChem tests from Spec 001 still pass.
- PubChem row mapping/skipping for hERG IC50 remains unchanged.
- A generic MeasurementInput can be produced from a mapped PubChem hERG IC50 row, either directly or through a conversion helper.
```

## Database changes

No database changes.

## Python/API changes

### 1. Add endpoint-config construction path

Add a classmethod, factory, or initializer pattern equivalent to:

```python
PubChemAdapter.from_source_config(
    endpoint: EndpointConfig,
    source_config: dict[str, Any],
    **runtime_options,
)
```

or:

```python
PubChemAdapter(endpoint=endpoint, source_config=source_config, **runtime_options)
```

Use the least disruptive form for the existing codebase.

### 2. Preserve old construction path

Existing scripts and tests that instantiate the adapter with explicit hERG parameters must continue to work.

If needed, implement old arguments as a wrapper that builds a source config internally.

### 3. Validate PubChem source config

Add lightweight validation for required PubChem keys:

```text
target_gene_symbol
target_gene_id
activity_name_regex
```

Validation should raise clear errors for missing required keys or invalid regex.

### 4. Preserve row filtering

For `herg_ic50`, the adapter should keep the current filter behavior:

```text
- accept rows for the configured target gene ID
- accept rows whose activity name matches the configured regex
- require the activity value fields currently required by the adapter
- skip wrong-target or wrong-activity rows as before
```

Do not broaden PubChem ingestion semantics in this spec.

### 5. Add generic measurement mapping hook

Add a narrow helper that converts a successfully mapped PubChem hERG IC50 row into `MeasurementInput`.

Acceptable approaches:

```text
- map_row continues returning the current staged record, and the staged record exposes measurement_input.
- map_row continues returning the current staged record, and a separate helper converts it to MeasurementInput.
- map_row returns a generic record only if the existing pipeline/tests are updated compatibly and behavior is preserved.
```

Prefer the first or second approach to minimize pipeline risk.

## Tests required

Tests must not call live PubChem.

Minimum tests:

```text
- Construct PubChem adapter using old explicit parameters; assert effective config matches existing behavior.
- Construct PubChem adapter from herg_ic50 endpoint source config; assert effective config matches old construction.
- Missing target_gene_symbol raises clear validation error.
- Missing target_gene_id raises clear validation error.
- Invalid activity_name_regex raises clear validation error.
- Fixture accepted KCNH2 IC50 row maps the same as in Spec 001.
- Fixture wrong-gene row is skipped as in Spec 001.
- Fixture wrong-activity-name row is skipped as in Spec 001.
- Accepted fixture row can be converted to MeasurementInput with measurement_type = IC50 and value_kind = concentration.
- Existing PubChem hERG tests still pass.
```

## Validation commands

Preferred:

```bash
python -m pytest
```

## Acceptance criteria

- [ ] PubChem adapter supports endpoint source config construction.
- [ ] Old PubChem construction path remains compatible.
- [ ] hERG IC50 PubChem behavior is unchanged.
- [ ] PubChem source config validation exists.
- [ ] PubChem mapped row can produce a `MeasurementInput` or equivalent generic measurement.
- [ ] No ChEMBL adapter changes were made.
- [ ] No pipeline write-path changes were made.
- [ ] Tests use fixtures/mocks, not live PubChem calls.

## Notes for Codex

- Keep this refactor narrow. The purpose is config sourcing, not a new PubChem ingestion strategy.
- Do not rename the old hERG script in this spec.
- Keep source config keys aligned with `endpoints.source_configs["pubchem"]` from Spec 002.
- Preserve current handling of `Activity Value [uM]` or equivalent fields; broader unit parsing comes in a later endpoint-expansion phase.
