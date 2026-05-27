# Endpoint bioactivity migration overview

## Purpose of this directory

This directory contains the implementation plan for migrating the current hERG IC50 database into a general endpoint-driven bioactivity system.

These documents are intended to be handed to Codex one at a time. Each numbered spec should produce one reviewable pull request. Codex should not implement later specs early, even when related code is nearby.

The intended workflow is:

```text
1. Read AGENTS.md.
2. Read docs/migration/000_overview.md.
3. Read exactly one numbered implementation spec.
4. Implement the smallest change that satisfies that spec.
5. Add or update tests required by that spec.
6. Run the validation commands that apply to the repository.
7. Summarize the diff, tests run, and any gaps.
```

## Migration goal

The current system is a SQL-first hERG IC50 database. The migration goal is to generalize it into a system that can ingest, normalize, store, browse, and export bioactivity data for arbitrary user-selected endpoints.

The generalized system should support endpoints such as:

```text
hERG IC50
CYP3A4 IC50
DRD2 Ki
EGFR EC50
hERG percent inhibition
Ames assay outcome
solubility
permeability
other source-defined bioactivity endpoints
```

The migration should preserve the current strengths of the repository:

```text
compound deduplication
external identifier resolution
source-record provenance
raw source payload retention
automatic ChEMBL and PubChem ingestion
normalized result browsing/export
```

The migration should remove the assumption that every stored measurement is a hERG IC50 value.

## Core design principle

Persist only the facts that must be durable for correctness, reproducibility, and auditability.

The required durable facts are:

```text
What molecule is this?
What external identifiers resolve it?
What exact source record did this come from?
What endpoint was the user trying to collect?
What normalized measurement was extracted?
When was ingestion run, with what source query, and what happened?
```

This leads to a deliberately small physical model:

```text
compounds
compound_identifiers
source_records
endpoints
bioactivity_results
ingestion_runs
```

The current schema also contains support tables such as `compound_names`, `compound_structure_assertions`, and `compound_identifier_sources`. These may remain, but they are not required to understand or implement the endpoint migration.

Do not add a target catalog, assay catalog, measurement ontology, source availability cache, `measurement_types` table, `endpoint_templates` table, or `endpoint_sources` table during the initial migration unless a later spec explicitly requests it.

## Target conceptual model

### `compounds`

Stores the internal molecule entity. One row represents one deduplicated compound known to the system.

Typical data:

```text
compound_id
canonical_smiles
standard_inchi
standard_inchikey
created_at
updated_at
```

Why it exists: bioactivity results need to attach to a stable internal compound, not directly to a PubChem CID, ChEMBL ID, name, or raw source row.

### `compound_identifiers`

Stores external database identifiers for compounds.

Each row means:

```text
This external identifier refers to this internal compound.
```

Typical data:

```text
compound_identifier_id
compound_id
namespace
identifier_value
normalized_value
is_primary
created_at
updated_at
```

Why it exists: identifiers are one-to-many. One compound can have a PubChem CID, ChEMBL molecule ID, UNII, CAS number, vendor ID, or future source-specific identifier. Keeping identifiers separate lets the database enforce uniqueness of a normalized identifier within a namespace.

### `source_records`

Stores raw external provenance.

Each row represents one external record pulled from a source such as ChEMBL or PubChem.

Typical data:

```text
source_record_id
source_name
source_record_key
record_type
source_release
source_url
raw_payload
created_at
updated_at
```

Why it exists: this is the provenance boundary. `source_records` stores what the external source said. `bioactivity_results` stores what this system extracted and normalized from that source record.

Do not merge `source_records` into `bioactivity_results`.

### `endpoints`

Stores a saved endpoint definition.

Each row represents one user-selected scientific question that the system can ingest.

A simplified table shape is:

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
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

`spec` stores the scientific meaning of the endpoint. `source_configs` stores source-specific query plans.

Example endpoint, abbreviated:

```json
{
  "endpoint_key": "herg_ic50",
  "display_name": "hERG IC50",
  "spec": {
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
  },
  "source_configs": {
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
}
```

Why it exists: result rows need to point to a durable endpoint definition. A loose text label such as `IC50` or `hERG IC50` is not enough to reconstruct what was queried or why a source row was included.

### `bioactivity_results`

Stores normalized measurements extracted from source records.

This table replaces the IC50-specific result model for new generalized ingestion.

A simplified table shape is:

```sql
CREATE TABLE bioactivity_results (
    result_id BIGSERIAL PRIMARY KEY,

    endpoint_id BIGINT NOT NULL REFERENCES endpoints(endpoint_id),
    compound_id BIGINT NOT NULL REFERENCES compounds(compound_id),
    source_record_id BIGINT NOT NULL REFERENCES source_records(source_record_id),
    ingestion_run_id BIGINT,

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

    UNIQUE (endpoint_id, source_record_id, result_key)
);
```

Why it exists: this is the core output of the system. It can represent IC50, Ki, EC50, percent inhibition, categorical outcomes, and future endpoint types without changing schema.

### `ingestion_runs`

Stores execution history, source query reproducibility, and QC summaries.

Each row means:

```text
The system attempted to ingest one endpoint from one source using this query configuration.
```

A simplified table shape is:

```sql
CREATE TABLE ingestion_runs (
    ingestion_run_id BIGSERIAL PRIMARY KEY,

    endpoint_id BIGINT NOT NULL REFERENCES endpoints(endpoint_id),

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
    error_summary JSONB NOT NULL DEFAULT '{}'::JSONB
);
```

Why it exists: ingestion is operational and repeatable. Users need to know when an endpoint was ingested, from which source, with which query config, and what happened.

## Optional support tables

The existing support tables may remain:

```text
compound_names
compound_structure_assertions
compound_identifier_sources
```

Treat them as support infrastructure, not as required migration concepts.

`compound_names` is useful for UI display and search, but names are not reliable identifiers.

`compound_structure_assertions` is useful for preserving source-specific structural claims and detecting source disagreements, but it is not required to generalize endpoints.

`compound_identifier_sources` is useful for identifier-level provenance, but `source_records.raw_payload` already preserves the underlying source evidence.

## Deferred tables and features

Do not add these during the first migration pass:

```text
target_entities
target_identifiers
source_targets
source_assays
source_measurement_availability
measurement_types
endpoint_templates
endpoint_sources
```

These may be introduced later if the system needs global endpoint discovery, source catalog browsing, or administrative ontology management.

For the initial migration, store target identity, measurement semantics, normalization rules, and source configs inside `endpoints.spec` and `endpoints.source_configs`, validated by Python code.

## Application-layer responsibilities

The following should remain in Python initially:

```text
endpoint spec validation
source config validation
unit normalization
relation/qualifier normalization
p-value computation
source-specific row filtering
source-specific result_key generation
preview logic
adapter-specific mapping
```

The database should store durable facts. The application should validate and interpret endpoint-specific logic.

## Implementation roadmap

The intended spec sequence is:

```text
001_baseline_tests.md
002_schema_generic_tables.md
003_generic_measurement_model.md
004_generic_result_upsert.md
005_endpoint_config_loading.md
006_refactor_chembl_adapter.md
007_refactor_pubchem_adapter.md
008_pipeline_writes_bioactivity_results.md
009_endpoint_preview_cli.md
010_streamlit_endpoint_ui.md
011_second_endpoint_smoke_test.md
012_deprecation_cleanup.md
```

All numbered specs in this sequence are now included in this migration pack.

## Migration acceptance criteria

The migration is complete when:

```text
1. hERG IC50 ingestion still works.
2. hERG IC50 data can be represented in bioactivity_results.
3. Raw external records remain traceable through source_records.
4. Every generalized result points to a saved endpoint.
5. Every ingestion run records source name, source release if available, query config, status, row counts, and QC summary.
6. ChEMBL and PubChem adapters read endpoint-specific configs instead of hard-coded hERG parameters.
7. The UI can select an endpoint before browsing, manual entry, upload, or export.
8. A second endpoint can be added without creating a new result table.
9. Non-IC50 measurements can be represented without schema changes.
10. Optional support tables remain useful but are not required to understand the core model.
```

## Non-negotiable guardrails

```text
Preserve existing hERG IC50 behavior until a spec explicitly changes it.
Do not drop ic50_results during the initial migration.
Do not remove existing scripts; convert them to wrappers only in a later spec.
Do not introduce deferred catalog/ontology tables early.
Do not make tests depend on live ChEMBL or PubChem network calls.
Do not merge source_records into bioactivity_results.
Do not store endpoint semantics only as free-text labels.
```
