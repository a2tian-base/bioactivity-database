# Spec 012: Deprecate IC50-specific paths after generic parity

## Goal

Clean up IC50-specific code paths after hERG IC50 and at least one second endpoint work through the generic endpoint-driven path.

This spec should reduce duplication and mark old APIs as compatibility wrappers. It should not remove important compatibility unexpectedly.

## Background

Earlier specs preserve hERG IC50 behavior while adding generic endpoint-driven ingestion. During the transition, the repository may have:

```text
old hERG-specific scripts
generic endpoint loading
dual-write to ic50_results and bioactivity_results
IC50-specific helpers and generic helpers side by side
UI support for both old and new result views
```

This spec consolidates those paths after parity is demonstrated.

## Preconditions

Do not implement this spec until all of the following are true:

```text
- hERG IC50 ingestion writes correct bioactivity_results.
- hERG IC50 existing behavior remains covered by regression tests.
- A second endpoint smoke test passes without schema changes.
- UI can browse generic endpoint results.
- Maintainers approve the chosen compatibility strategy.
```

## Non-goals

- Do not drop `ic50_results` unless explicitly approved in the active implementation prompt.
- Do not remove old script names if users may still call them.
- Do not remove hERG IC50 tests.
- Do not add new endpoint types.
- Do not add source catalog, target catalog, or ontology tables.
- Do not perform broad package renaming unless tests and compatibility wrappers make it safe.

## Files likely to change

Likely files:

```text
app/herg/scripts/ingest_chembl_herg.py
app/herg/scripts/ingest_pubchem_herg.py
app/herg/pipeline.py
app/bioactivity/...
app/app.py
db/init/001_schema.sql              # only for compatibility views, not table drops unless approved
tests/...
docs/...
```

## Required behavior

After this spec is implemented:

```text
- Generic endpoint ingestion is the primary path.
- Old hERG script names remain as wrappers around generic endpoint ingestion.
- Direct writes to ic50_results are either stopped or explicitly retained with a clear reason.
- If direct writes to ic50_results stop, an IC50 compatibility view or documented migration path exists.
- Deprecated functions emit clear warnings or are marked in documentation.
- Tests confirm old entry points still work.
```

## Database changes

No required structural database changes.

Potential database cleanup options, subject to maintainer approval:

```text
Option A: Keep ic50_results as a legacy table and retain dual-write.
Option B: Stop writing ic50_results and create ic50_results_compat_v over bioactivity_results.
Option C: Keep ic50_results unchanged and document it as legacy-only.
```

Do not drop `ic50_results` in this spec unless the active implementation prompt explicitly says to do so.

If adding a compatibility view, use a shape equivalent to:

```sql
CREATE VIEW ic50_results_compat_v AS
SELECT
    r.result_id,
    r.compound_id,
    r.source_record_id,
    e.endpoint_key AS endpoint,
    r.original_value AS ic50_value,
    r.original_unit AS ic50_unit,
    r.original_relation AS qualifier,
    r.standard_value AS ic50_um,
    r.p_value AS pic50,
    r.p_value_relation AS pic50_qualifier,
    r.created_at,
    r.updated_at
FROM bioactivity_results r
JOIN endpoints e ON e.endpoint_id = r.endpoint_id
WHERE r.measurement_type = 'IC50'
  AND r.value_kind = 'concentration';
```

Adjust field names to match the actual implemented schema.

## Python/API changes

### 1. Convert old scripts to wrappers

Old scripts should remain callable but delegate to generic endpoint ingestion.

Conceptual behavior:

```python
# ingest_chembl_herg.py
run_endpoint_ingestion(endpoint_key="herg_ic50", source_name="chembl", ...)

# ingest_pubchem_herg.py
run_endpoint_ingestion(endpoint_key="herg_ic50", source_name="pubchem", ...)
```

Preserve existing CLI arguments where possible. If arguments are superseded by endpoint config, keep them as overrides only if that is safe and documented.

### 2. Mark old helpers as deprecated

If old IC50-specific helpers remain, document them as compatibility helpers.

Use warnings only if they will not create noisy test output or break expected behavior. Otherwise, use docstrings/comments.

### 3. Consolidate duplicated normalization

If generic and IC50-specific normalization now duplicate logic, consolidate into one implementation only if tests make the change safe.

Do not change numerical semantics.

### 4. Update documentation

Update README or migration docs to state:

```text
- generic endpoint ingestion is the preferred path
- hERG scripts are compatibility wrappers
- ic50_results is legacy or compatibility-only, depending on chosen strategy
```

## Tests required

Minimum tests:

```text
- Old ChEMBL hERG script entry point delegates to generic endpoint ingestion or still produces equivalent behavior.
- Old PubChem hERG script entry point delegates to generic endpoint ingestion or still produces equivalent behavior.
- Generic ingestion path still writes bioactivity_results.
- hERG IC50 regression tests still pass.
- Second endpoint smoke test still passes.
- If compatibility view is added, it returns expected hERG IC50 fields from bioactivity_results.
- No code path writes inconsistent duplicate values between ic50_results and bioactivity_results, if dual-write remains.
```

Use mocks/fakes for script-level tests. Do not call live external services.

## Validation commands

Preferred:

```bash
python -m pytest
```

Run any repository lint/type-check command if one exists.

## Acceptance criteria

- [ ] Generic endpoint ingestion is the documented primary path.
- [ ] Old hERG scripts remain callable.
- [ ] IC50-specific functions are either wrappers, documented legacy paths, or safely retained.
- [ ] Direct `ic50_results` write strategy is explicit.
- [ ] No approved compatibility behavior is broken.
- [ ] hERG and second-endpoint tests pass.
- [ ] Documentation reflects the new primary workflow.

## Notes for Codex

- This is a cleanup spec, not an opportunity for broad redesign.
- Keep compatibility unless the maintainer explicitly authorizes removal.
- Prefer documented wrappers over deleting old code.
- Do not drop database objects without explicit approval.
