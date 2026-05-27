# Spec 002: Add generic endpoint, result, and ingestion-run tables

## Goal

Add the generic database tables required for endpoint-driven bioactivity ingestion:

```text
endpoints
bioactivity_results
ingestion_runs
```

Seed the first endpoint, `herg_ic50`, using the simplified JSON-based endpoint model.

This spec should not change current hERG IC50 ingestion behavior. It only adds schema and seed data for the generalized path.

## Background

The current result model is centered on `ic50_results`. That table is appropriate for hERG IC50 but cannot cleanly represent other endpoint types such as Ki, EC50, percent inhibition, categorical assay outcomes, solubility, or permeability.

The target core model keeps existing identity/provenance tables:

```text
compounds
compound_identifiers
source_records
```

and adds:

```text
endpoints
bioactivity_results
ingestion_runs
```

Do not add `endpoint_sources`. Store source-specific query configs in `endpoints.source_configs` for now.

## Non-goals

- Do not refactor ChEMBL or PubChem adapters.
- Do not modify the ingestion pipeline to write `bioactivity_results` yet.
- Do not modify Streamlit UI.
- Do not remove, rename, or drop `ic50_results`.
- Do not remove existing SQL functions or views.
- Do not add `endpoint_sources`.
- Do not add target catalog, assay catalog, measurement ontology, or source availability tables.
- Do not add `measurement_types` or `endpoint_templates` tables.
- Do not change existing hERG IC50 behavior.

## Files likely to change

Codex should inspect the repository's database initialization and migration pattern first.

Likely files:

```text
db/init/001_schema.sql
```

Test files may be added or updated under the repository's test directory.

If the repository has a separate migration mechanism, use it consistently. If the schema is currently managed only through `db/init/001_schema.sql`, add the new DDL there in a clearly labeled section.

## Required behavior

After this spec is implemented:

```text
- A fresh database initialization creates endpoints.
- A fresh database initialization creates ingestion_runs.
- A fresh database initialization creates bioactivity_results.
- endpoints contains a seeded active herg_ic50 endpoint.
- Existing tables, including ic50_results, remain available.
- Existing hERG IC50 ingestion behavior is unchanged.
- bioactivity_results can represent concentration, percent, numeric, categorical, and text value kinds.
- ingestion_runs can record source, source release, query config, status, row counts, QC summary, and error summary.
```

## Database changes

### 1. Add `endpoints`

Add a table equivalent to:

```sql
CREATE TABLE endpoints (
    endpoint_id BIGSERIAL PRIMARY KEY,
    endpoint_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,

    spec JSONB NOT NULL,
    source_configs JSONB NOT NULL DEFAULT '{}'::JSONB,

    spec_hash TEXT NOT NULL UNIQUE,
    active BOOLEAN NOT NULL DEFAULT TRUE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CHECK (btrim(endpoint_key) <> ''),
    CHECK (btrim(display_name) <> ''),
    CHECK (jsonb_typeof(spec) = 'object'),
    CHECK (jsonb_typeof(source_configs) = 'object')
);
```

Add an `updated_at` trigger if the existing schema uses that pattern.

Recommended indexes:

```sql
CREATE INDEX ... ON endpoints(active);
```

Do not over-index JSON fields yet.

### 2. Seed `herg_ic50`

Insert one active endpoint row for hERG IC50.

The endpoint should use:

```text
endpoint_key: herg_ic50
display_name: hERG IC50
```

The `spec` JSON should contain at least:

```json
{
  "target": {
    "preferred_name": "hERG",
    "gene_symbol": "KCNH2",
    "organism": "Homo sapiens",
    "identifiers": {
      "chembl_target_id": "CHEMBL240",
      "ncbi_gene_id": "3757"
    }
  },
  "measurement": {
    "type": "IC50",
    "value_kind": "concentration",
    "canonical_unit": "uM",
    "supports_p_value": true,
    "p_value_name": "pIC50"
  },
  "normalization": {
    "allowed_units": ["pM", "nM", "uM", "mM"],
    "allowed_relations": ["=", "<", ">"]
  },
  "inclusion_criteria": {
    "organism": "Homo sapiens",
    "direct_target_only": true
  }
}
```

The `source_configs` JSON should contain at least:

```json
{
  "chembl": {
    "target_chembl_id": "CHEMBL240",
    "standard_type": "IC50",
    "standard_relation__in": ["=", "<", ">"],
    "data_validity_comment__isnull": true
  },
  "pubchem": {
    "target_gene_symbol": "KCNH2",
    "target_gene_id": "3757",
    "activity_name_regex": "(?i)\\bIC50\\b"
  }
}
```

`spec_hash` should be deterministic. Use an existing hash helper if present. If not, use a simple deterministic SQL expression such as `md5(spec::text || '|' || source_configs::text)` for seed data, without adding a new extension dependency.

Use an idempotent insert/update pattern if the existing init script uses such a pattern. Otherwise use the repository's existing seed-data convention.

### 3. Add `ingestion_runs`

Add a table equivalent to:

```sql
CREATE TABLE ingestion_runs (
    ingestion_run_id BIGSERIAL PRIMARY KEY,

    endpoint_id BIGINT NOT NULL REFERENCES endpoints(endpoint_id) ON DELETE RESTRICT,

    source_name TEXT NOT NULL,
    source_release TEXT,

    query_config JSONB NOT NULL DEFAULT '{}'::JSONB,
    query_hash TEXT NOT NULL,

    status TEXT NOT NULL CHECK (
        status IN ('running', 'succeeded', 'failed', 'partial')
    ),

    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,

    rows_seen INTEGER NOT NULL DEFAULT 0,
    rows_inserted INTEGER NOT NULL DEFAULT 0,
    rows_updated INTEGER NOT NULL DEFAULT 0,
    rows_skipped INTEGER NOT NULL DEFAULT 0,
    rows_failed INTEGER NOT NULL DEFAULT 0,

    qc_summary JSONB NOT NULL DEFAULT '{}'::JSONB,
    error_summary JSONB NOT NULL DEFAULT '{}'::JSONB,

    CHECK (btrim(source_name) <> ''),
    CHECK (btrim(query_hash) <> ''),
    CHECK (jsonb_typeof(query_config) = 'object'),
    CHECK (jsonb_typeof(qc_summary) = 'object'),
    CHECK (jsonb_typeof(error_summary) = 'object'),
    CHECK (rows_seen >= 0),
    CHECK (rows_inserted >= 0),
    CHECK (rows_updated >= 0),
    CHECK (rows_skipped >= 0),
    CHECK (rows_failed >= 0),
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);
```

Recommended indexes:

```sql
CREATE INDEX ... ON ingestion_runs(endpoint_id, source_name, started_at DESC);
CREATE INDEX ... ON ingestion_runs(status);
```

### 4. Add `bioactivity_results`

Add a table equivalent to:

```sql
CREATE TABLE bioactivity_results (
    result_id BIGSERIAL PRIMARY KEY,

    endpoint_id BIGINT NOT NULL REFERENCES endpoints(endpoint_id) ON DELETE RESTRICT,
    compound_id BIGINT NOT NULL REFERENCES compounds(compound_id) ON DELETE RESTRICT,
    source_record_id BIGINT NOT NULL REFERENCES source_records(source_record_id) ON DELETE RESTRICT,
    ingestion_run_id BIGINT REFERENCES ingestion_runs(ingestion_run_id) ON DELETE SET NULL,

    result_key TEXT NOT NULL,

    measurement_type TEXT NOT NULL,
    value_kind TEXT NOT NULL CHECK (
        value_kind IN ('concentration', 'percent', 'numeric', 'categorical', 'text')
    ),

    original_value NUMERIC,
    original_unit TEXT,
    original_relation TEXT,

    standard_value NUMERIC,
    standard_unit TEXT,
    standard_relation TEXT,

    p_value NUMERIC,
    p_value_relation TEXT,

    value_text TEXT,

    assay_context JSONB NOT NULL DEFAULT '{}'::JSONB,
    quality_flags JSONB NOT NULL DEFAULT '{}'::JSONB,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (endpoint_id, source_record_id, result_key),

    CHECK (btrim(result_key) <> ''),
    CHECK (btrim(measurement_type) <> ''),
    CHECK (jsonb_typeof(assay_context) = 'object'),
    CHECK (jsonb_typeof(quality_flags) = 'object')
);
```

Add an `updated_at` trigger if the existing schema uses that pattern.

Recommended indexes:

```sql
CREATE INDEX ... ON bioactivity_results(endpoint_id);
CREATE INDEX ... ON bioactivity_results(compound_id);
CREATE INDEX ... ON bioactivity_results(source_record_id);
CREATE INDEX ... ON bioactivity_results(ingestion_run_id);
CREATE INDEX ... ON bioactivity_results(measurement_type);
```

Do not add endpoint-specific generated columns for pIC50 in this generic table. p-value fields are nullable and conditional.

### 5. Preserve `ic50_results`

Do not remove or alter `ic50_results` in this spec unless a minimal compatibility reference is necessary. Existing hERG behavior must remain intact.

No compatibility view is required in this spec. That comes later when write paths move to `bioactivity_results`.

## Python/API changes

No production Python/API changes are required.

It is acceptable to add a very small test helper or fixture loader if needed for schema tests.

Do not modify the ChEMBL adapter, PubChem adapter, pipeline, or Streamlit UI in this spec.

## Tests required

Add database schema tests using the repository's existing test strategy.

Tests should verify:

```text
- endpoints table exists.
- ingestion_runs table exists.
- bioactivity_results table exists.
- herg_ic50 endpoint seed row exists.
- herg_ic50 spec contains measurement.type = IC50.
- herg_ic50 spec contains measurement.value_kind = concentration.
- herg_ic50 source_configs contains chembl and pubchem objects.
- invalid endpoint spec/source_configs JSON types are rejected.
- invalid ingestion_run status is rejected.
- negative row counts are rejected.
- invalid bioactivity_results value_kind is rejected.
- duplicate (endpoint_id, source_record_id, result_key) is rejected.
```

If the repository has no database test harness, add a minimal SQL smoke test file under an appropriate test or db directory and document how to run it. Do not silently skip schema validation.

Do not use SQLite for these tests. The new schema depends on PostgreSQL features such as JSONB and PostgreSQL constraints.

## Validation commands

Codex should run the repository's existing validation commands.

Preferred if available:

```bash
python -m pytest
```

For database validation, use the repository's existing PostgreSQL/docker workflow if present.

If no database test environment is available, Codex must still provide a SQL smoke-test script and state that it was not executed, including the reason.

## Acceptance criteria

- [ ] `endpoints` table is added.
- [ ] `ingestion_runs` table is added.
- [ ] `bioactivity_results` table is added.
- [ ] `herg_ic50` endpoint is seeded.
- [ ] `herg_ic50` contains ChEMBL and PubChem source configs.
- [ ] `ic50_results` remains available and unchanged for current workflows.
- [ ] No deferred catalog/ontology tables were added.
- [ ] No production ingestion code was refactored.
- [ ] Schema tests or SQL smoke tests were added.
- [ ] Validation commands and any execution limitations are documented in the PR summary.

## Notes for Codex

- Inspect existing schema conventions before editing SQL.
- Reuse existing timestamp trigger patterns if present.
- Use table/constraint/index naming consistent with the current schema.
- Keep JSON structure stable and explicit; later specs will load endpoint configs from this seed row.
- Keep this PR schema-only except for tests.
- This spec prepares the database for the generic pipeline; it does not activate the generic pipeline yet.
