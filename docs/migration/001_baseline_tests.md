# Spec 001: Baseline hERG IC50 regression tests

## Goal

Add regression tests and fixtures that capture the current hERG IC50 behavior before any schema or pipeline migration occurs.

This spec should not change production behavior. It creates the safety net for later migration specs.

## Background

The repository currently implements a hERG IC50-focused workflow. The existing system includes:

```text
db/init/001_schema.sql
app/herg/pipeline.py
app/herg/sources/chembl.py
app/herg/sources/pubchem.py
app/app.py
```

The migration will replace the IC50-specific result model with a generic endpoint-driven model. Before that happens, current behavior must be captured so later PRs can prove they did not accidentally alter hERG IC50 semantics.

The baseline should focus on the behaviors most likely to regress:

```text
compound registration / identifier resolution
source record upsert semantics
IC50 unit normalization
pIC50 calculation
qualifier inversion
ChEMBL hERG row mapping
PubChem hERG row mapping
pipeline skip/error handling
```

## Non-goals

- Do not change database schema.
- Do not add `endpoints`, `bioactivity_results`, or `ingestion_runs` yet.
- Do not refactor ChEMBL or PubChem adapters.
- Do not modify Streamlit UI.
- Do not change ingestion outputs.
- Do not remove or rename existing hERG code.
- Do not add live network calls to tests.
- Do not add deferred catalog/ontology tables.

## Files likely to change

Codex should inspect the repository first and follow existing test conventions. Likely additions include:

```text
tests/
tests/fixtures/
tests/fixtures/herg_ic50/
```

Likely files under test:

```text
app/herg/pipeline.py
app/herg/sources/chembl.py
app/herg/sources/pubchem.py
db/init/001_schema.sql
```

Do not change production files unless a very small change is needed to make existing behavior testable. If a production change is needed, keep it mechanical and explain it in the PR summary.

## Required behavior

After this spec is implemented:

```text
- The repository has deterministic tests for current hERG IC50 behavior.
- Tests do not call live ChEMBL, PubChem, UniChem, or any external service.
- Tests use fixtures or mocks for representative source rows.
- The tests can be run locally through the repository's normal test command.
- Existing hERG IC50 code paths continue to behave as before.
```

## Database changes

No database schema changes.

If the repository already has a PostgreSQL integration-test pattern, Codex may add integration tests that initialize the current schema and assert existing IC50 functions/generated columns behave as expected.

If the repository does not have a database test harness, do not invent a large one in this spec. Add pure-Python tests and fixture expectations first, and document the database-test gap in the PR summary.

## Python/API changes

No intended production API changes.

Small testability-only changes are acceptable if necessary, for example:

```text
- exposing an already-existing pure function through an importable module
- splitting a pure normalization helper without changing behavior
- adding type hints or constants needed by tests
```

Do not change adapter behavior.

## Fixtures to add

Add a small set of deterministic fixtures representing source rows. Keep them minimal but realistic enough to exercise current mapping behavior.

Recommended fixture directory:

```text
tests/fixtures/herg_ic50/
  chembl_activity_ic50_equal.json
  chembl_activity_ic50_less_than.json
  pubchem_concise_ic50_equal.json
  pubchem_concise_wrong_gene.json
  pubchem_concise_wrong_activity_name.json
```

The exact fixture shape should match the current adapter inputs. Do not invent fields that the current adapter does not use.

## Tests required

Add tests in the repository's existing framework. If there is no existing framework, prefer `pytest`.

### 1. IC50 unit normalization

Test current concentration-unit behavior for all currently accepted units.

Expected cases:

```text
pM -> uM
nM -> uM
uM -> uM
mM -> uM
```

If normalization currently lives only in SQL generated columns, test it through a database integration test if available. Otherwise create expected-value fixtures and note that SQL execution coverage will be added in the schema spec.

### 2. pIC50 calculation

Test current pIC50 derivation for representative values.

Example expectations:

```text
1 uM   -> pIC50 6
100 nM -> pIC50 7
10 uM  -> pIC50 5
```

Use the current system's precision/rounding behavior. Do not introduce a new rounding policy.

### 3. Qualifier inversion

Test the current inversion logic for p-value qualifiers.

Expected semantics:

```text
IC50 < x  -> pIC50 > y
IC50 > x  -> pIC50 < y
IC50 = x  -> pIC50 = y
```

Use the exact qualifier representation already used by the system.

### 4. Source record upsert behavior

If there is an existing database helper or integration-test setup, test that upserting the same `(source_name, source_record_key)` does not create duplicate source records and preserves/updates fields according to current behavior.

If this cannot be tested without introducing a database harness, document the gap.

### 5. Compound identifier resolution

Test that the current compound registration / identifier resolution behavior is stable for at least:

```text
PubChem CID
ChEMBL molecule ID
standard InChIKey, if currently supported by code path
```

Use existing helper functions. Do not rewrite compound resolution.

### 6. ChEMBL adapter mapping

Using fixture rows, test that the current ChEMBL adapter maps a hERG IC50 activity row into the expected staged record / IC50 input.

Cover at least:

```text
accepted IC50 row
accepted inequality row
row skipped because it lacks required compound identity or measurement value, if current adapter skips such rows
```

Do not make a live ChEMBL request.

### 7. PubChem adapter mapping

Using fixture rows, test that the current PubChem adapter maps or skips concise bioactivity rows according to current hERG IC50 logic.

Cover at least:

```text
accepted KCNH2 IC50 row
row skipped for wrong target gene ID
row skipped for wrong activity name
row skipped for missing activity value, if current adapter handles that case
```

Do not make a live PubChem request.

### 8. Pipeline skip/error behavior

Test one representative pipeline path where a bad row is skipped or recorded as an error according to current behavior.

Keep this narrow. Do not rewrite the pipeline.

## Validation commands

Codex should discover and run the repository's existing test command.

Preferred command if no repository-specific command exists:

```bash
python -m pytest
```

If only a subset of tests can run due to missing local services, run the deterministic unit tests and report the limitation.

## Acceptance criteria

- [ ] No database schema changes were made.
- [ ] No production behavior intentionally changed.
- [ ] Deterministic fixtures for ChEMBL and PubChem hERG IC50 mapping were added.
- [ ] Tests cover IC50 unit normalization or document why SQL-only behavior could not yet be executed.
- [ ] Tests cover pIC50 calculation or document why SQL-only behavior could not yet be executed.
- [ ] Tests cover qualifier inversion or document why SQL-only behavior could not yet be executed.
- [ ] Tests cover at least one accepted ChEMBL row and one accepted PubChem row.
- [ ] Tests cover at least one skipped/rejected source row.
- [ ] Tests do not call live external APIs.
- [ ] The PR summary lists all validation commands run and any gaps.

## Notes for Codex

- Inspect existing code before choosing fixture shapes.
- Prefer testing current public functions/methods instead of introducing new abstraction layers.
- Do not overfit tests to private implementation details if a stable public helper exists.
- Keep fixture payloads small, but include the fields actually consumed by the current adapters.
- If a behavior exists only in PostgreSQL generated columns or SQL functions, use a DB integration test only if the repository already has a clear pattern for it.
- This spec is a safety-net PR. Keep it boring.
