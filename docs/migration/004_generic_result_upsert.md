# Spec 004: Add generic bioactivity result write path

## Goal

Add a tested write path for inserting and updating rows in `bioactivity_results` using the generic measurement model from Spec 003.

This spec should make the new table usable by code, but it should not yet change ChEMBL, PubChem, or the ingestion pipeline to write generic results.

## Background

Spec 002 adds the `bioactivity_results` table. Spec 003 adds a generic `MeasurementInput` model.

The current system writes IC50 measurements through IC50-specific functions or helpers. The migration needs a generic write path that can store measurements for any endpoint, while preserving links to:

```text
endpoint_id
compound_id
source_record_id
ingestion_run_id
```

## Non-goals

- Do not refactor ChEMBL or PubChem adapters.
- Do not modify the main ingestion pipeline to call this write path yet.
- Do not remove or change `upsert_ic50_result`.
- Do not drop or modify `ic50_results`.
- Do not modify Streamlit UI.
- Do not add endpoint discovery or catalog tables.
- Do not make tests call live external services.

## Files likely to change

Codex should inspect existing database access conventions first.

Likely files:

```text
app/.../db.py
app/.../repository.py
app/.../models.py
app/bioactivity/db.py
app/bioactivity/results.py
tests/...
```

If the existing code uses SQL functions for upserts, Codex may add a SQL function. If the existing code uses Python-level SQL, Codex may add a Python helper instead. Follow repository conventions.

## Required behavior

After this spec is implemented:

```text
- Code can upsert one MeasurementInput into bioactivity_results.
- Upsert requires endpoint_id, compound_id, source_record_id, and result_key.
- Upsert can associate the result with an ingestion_run_id.
- Re-upserting the same (endpoint_id, source_record_id, result_key) updates the existing row instead of inserting a duplicate.
- The write path supports concentration, percent, numeric, categorical, and text value kinds.
- The write path does not affect ic50_results.
```

## Database changes

No new tables.

If repository convention favors SQL functions, add a function equivalent to:

```sql
upsert_bioactivity_result(
    p_endpoint_id BIGINT,
    p_compound_id BIGINT,
    p_source_record_id BIGINT,
    p_ingestion_run_id BIGINT,
    p_result_key TEXT,
    p_measurement_type TEXT,
    p_value_kind TEXT,
    p_original_value NUMERIC,
    p_original_unit TEXT,
    p_original_relation TEXT,
    p_standard_value NUMERIC,
    p_standard_unit TEXT,
    p_standard_relation TEXT,
    p_p_value NUMERIC,
    p_p_value_relation TEXT,
    p_value_text TEXT,
    p_assay_context JSONB,
    p_quality_flags JSONB
) RETURNS BIGINT
```

The function should:

```text
- insert or update based on UNIQUE (endpoint_id, source_record_id, result_key)
- return result_id
- update updated_at on conflict
- preserve created_at on conflict
```

If repository convention favors Python-level SQL, implement the same behavior in Python with parameterized SQL.

Do not add generated pIC50 columns to `bioactivity_results`.

## Python/API changes

Add a function equivalent to:

```python
def upsert_bioactivity_result(
    conn,
    *,
    endpoint_id: int,
    compound_id: int,
    source_record_id: int,
    ingestion_run_id: int | None,
    measurement: MeasurementInput,
) -> int:
    ...
```

Adjust the signature to match existing connection/session patterns.

The function should serialize `assay_context` and `quality_flags` as JSONB-compatible objects.

It should not compute endpoint-specific values. The caller should pass already-normalized fields in `MeasurementInput`.

## Tests required

Use the repository's PostgreSQL test strategy. Do not use SQLite for these tests.

Tests should set up or reuse minimal rows for:

```text
endpoint
compound
source_record
ingestion_run, if needed
```

Minimum tests:

```text
- Insert concentration MeasurementInput into bioactivity_results.
- Insert percent MeasurementInput into bioactivity_results.
- Insert categorical MeasurementInput into bioactivity_results.
- Re-upsert same endpoint_id/source_record_id/result_key updates row and returns same result_id.
- Duplicate result_key under a different endpoint_id is allowed.
- Duplicate result_key under same endpoint_id/source_record_id is not duplicated.
- assay_context and quality_flags round-trip as JSON objects.
- invalid value_kind is rejected by schema or model validation.
- ic50_results row count is unchanged by generic result upsert.
```

If a full PostgreSQL test harness is not available, add a minimal SQL smoke test and document execution limitations.

## Validation commands

Preferred:

```bash
python -m pytest
```

For database-specific tests, use the repository's docker/PostgreSQL workflow if available.

## Acceptance criteria

- [ ] Generic upsert function/helper exists.
- [ ] Upsert returns `result_id`.
- [ ] Upsert is idempotent for `(endpoint_id, source_record_id, result_key)`.
- [ ] The write path supports at least concentration, percent, and categorical examples.
- [ ] `ic50_results` is not modified by this helper.
- [ ] Tests cover insert and update behavior.
- [ ] No source adapters or pipeline write paths were refactored.

## Notes for Codex

- Inspect the existing `upsert_ic50_result` implementation before designing the generic upsert.
- Reuse existing transaction patterns and parameter binding style.
- Keep the helper small. This spec is not a full repository refactor.
- Do not silently swallow database constraint errors; tests should reveal invalid records.
