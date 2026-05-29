# Database Tables

The canonical schema is `db/init/001_schema.sql`. Docker runs it only when PostgreSQL initializes a fresh `db_data` volume.

The core post-migration tables are `compounds`, `compound_identifiers`, `source_records`, `endpoints`, `bioactivity_results`, and `ingestion_runs`. hERG IC50 compatibility and enrichment support tables remain in the same schema.

## Core Tables

### `compounds`

One deduplicated molecule.

| Field | Description |
| --- | --- |
| `compound_id` | Primary key. |
| `canonical_smiles` | Optional canonical SMILES. |
| `standard_inchi` | Optional standard InChI. |
| `standard_inchikey` | Optional standard InChIKey; unique when present. |
| `created_at`, `updated_at` | Row timestamps. |

### `compound_identifiers`

External or internal identifiers attached to a compound.

| Field | Description |
| --- | --- |
| `compound_identifier_id` | Primary key. |
| `compound_id` | Compound foreign key. |
| `namespace` | Identifier namespace, for example `chembl_id`, `pubchem_cid`, `unii`, `a_number`, or `standard_inchikey`. |
| `identifier_value` | Original identifier value. |
| `normalized_value` | Generated value used for matching and uniqueness. |
| `is_primary` | Preferred identifier for the compound within the namespace. |
| `created_at`, `updated_at` | Row timestamps. |

Key rules: `(namespace, normalized_value)` is unique; only one primary identifier is allowed per `(compound_id, namespace)`.

### `source_records`

Raw provenance boundary for manual rows, CSV rows, source measurements, and enrichment assertions.

| Field | Description |
| --- | --- |
| `source_record_id` | Primary key. |
| `source_name` | Source or workflow name, for example `chembl`, `pubchem`, `manual`, `identifier_csv`, or `unichem`. |
| `source_record_key` | Stable source-specific record key. |
| `record_type` | Workflow type, for example `activity`, `manual_entry`, `csv_import`, or `structure_enrichment`. |
| `source_release` | Optional source version or release. |
| `source_url` | Optional source URL. |
| `raw_payload` | JSONB copy of the source row or API response. |
| `created_at`, `updated_at` | Row timestamps. |

Key rule: `(source_name, source_record_key)` is unique.

### `endpoints`

Saved scientific endpoint definitions.

| Field | Description |
| --- | --- |
| `endpoint_id` | Primary key. |
| `endpoint_key` | Stable unique key such as `herg_ic50`. |
| `display_name` | Human-readable endpoint name. |
| `spec` | JSONB scientific definition: target, measurement, normalization, and inclusion criteria. |
| `source_configs` | JSONB source-specific query configuration for adapters such as ChEMBL and PubChem. |
| `spec_hash` | Unique hash of the endpoint definition. |
| `active` | Whether the endpoint appears in the app selector. |
| `created_at`, `updated_at` | Row timestamps. |

Currently seeded examples include `herg_ic50` and `cyp3a4_ic50`.

### `bioactivity_results`

Primary normalized measurement table.

| Field | Description |
| --- | --- |
| `result_id` | Primary key. |
| `endpoint_id` | Endpoint foreign key. |
| `compound_id` | Compound foreign key. |
| `source_record_id` | Source provenance foreign key. |
| `ingestion_run_id` | Optional ingestion run foreign key. |
| `result_key` | Stable key for one measurement within the source record. |
| `measurement_type` | Measurement label, for example `IC50`, `Ki`, or `percent inhibition`. |
| `value_kind` | One of `concentration`, `percent`, `numeric`, `categorical`, or `text`. |
| `original_value` | Numeric value as reported by the source. |
| `original_unit` | Unit as reported by the source. |
| `original_relation` | Relation as reported by the source, for example `=`, `<`, or `>`. |
| `standard_value` | Normalized numeric value. |
| `standard_unit` | Normalized unit. |
| `standard_relation` | Normalized relation. |
| `p_value` | Optional potency-style transformed value such as pIC50. |
| `p_value_relation` | Relation for `p_value`. |
| `value_text` | Text or categorical value when no numeric value applies. |
| `assay_context` | JSONB assay metadata retained with the normalized result. |
| `quality_flags` | JSONB warnings, exclusions, or validation flags. |
| `created_at`, `updated_at` | Row timestamps. |

Key rule: `(endpoint_id, source_record_id, result_key)` is unique.

### `ingestion_runs`

Audit trail for source ingestion jobs.

| Field | Description |
| --- | --- |
| `ingestion_run_id` | Primary key. |
| `endpoint_id` | Endpoint foreign key. |
| `source_name` | Source adapter name. |
| `source_release` | Optional source release. |
| `query_config` | JSONB effective source query configuration. |
| `query_hash` | Hash of the query configuration. |
| `status` | `running`, `succeeded`, `failed`, or `partial`. |
| `started_at`, `finished_at` | Run timestamps. |
| `rows_seen` | Source rows inspected. |
| `rows_inserted` | Result rows inserted. |
| `rows_updated` | Result rows updated. |
| `rows_skipped` | Source rows skipped. |
| `rows_failed` | Source rows that failed processing. |
| `qc_summary` | JSONB quality-control summary. |
| `error_summary` | JSONB error summary. |

## Support Tables

### `compound_names`

Preferred names and aliases for compounds.

| Field | Description |
| --- | --- |
| `compound_name_id` | Primary key. |
| `compound_id` | Compound foreign key. |
| `name` | Original name. |
| `normalized_name` | Generated lowercased, whitespace-normalized name. |
| `name_type` | Name category, usually `preferred` or `alias`. |
| `is_preferred` | Preferred display name flag. |
| `created_at`, `updated_at` | Row timestamps. |

### `compound_identifier_sources`

Join table showing which source records support compound identifiers.

| Field | Description |
| --- | --- |
| `compound_identifier_source_id` | Primary key. |
| `compound_identifier_id` | Identifier foreign key. |
| `source_record_id` | Source record foreign key. |
| `created_at` | Row timestamp. |

### `compound_structure_assertions`

Source-backed structure assertions from enrichment workflows.

| Field | Description |
| --- | --- |
| `compound_structure_assertion_id` | Primary key. |
| `compound_id` | Compound foreign key. |
| `source_record_id` | Source record foreign key. |
| `canonical_smiles` | Source-provided canonical SMILES. |
| `standard_inchi` | Source-provided standard InChI. |
| `standard_inchikey` | Source-provided standard InChIKey. |
| `created_at` | Row timestamp. |

### `ic50_results`

Legacy hERG IC50 result table retained for compatibility.

| Field | Description |
| --- | --- |
| `result_id` | Primary key. |
| `compound_id` | Compound foreign key. |
| `source_record_id` | Source record foreign key. |
| `endpoint` | Legacy endpoint label, default `IC50`. |
| `ic50_value` | Positive source value. |
| `ic50_unit` | `pM`, `nM`, `uM`, or `mM`. |
| `qualifier` | `=`, `<`, or `>`. |
| `ic50_um` | Generated micromolar value. |
| `pic50` | Generated pIC50 value. |
| `pic50_qualifier` | Generated inverted qualifier for pIC50. |
| `created_at`, `updated_at` | Row timestamps. |

IC50 ingestion paths dual-write hERG IC50 data to `ic50_results` and `bioactivity_results`.

## Read Views

- `compound_summary_v`: one row per compound with display identifiers, preferred name, aliases, and structure fields.
- `ic50_result_summary_v`: legacy hERG IC50 result view with compound labels and source provenance.

## Database Functions

Application code uses database helper functions for common writes and normalization:

- `register_compound_v2(...)`
- `resolve_compound_id(...)`
- `resolve_compound_by_keys(...)`
- `upsert_source_record(...)`
- `upsert_ic50_result(...)`
- `upsert_bioactivity_result(...)`
- `normalize_identifier(...)`
- `normalize_name(...)`
- `convert_to_um(...)`
- `invert_qualifier(...)`
