# Data Model

The canonical schema lives in `db/init/001_schema.sql`. Docker runs this file only when PostgreSQL initializes a fresh `db_data` volume.

## Tables

### `compounds`

One row per resolved compound.

| Column | Notes |
| --- | --- |
| `compound_id` | Primary key. |
| `canonical_smiles` | Optional canonical SMILES. |
| `standard_inchi` | Optional standard InChI. |
| `standard_inchikey` | Optional standard InChIKey; unique when present after trimming and uppercasing. |
| `created_at`, `updated_at` | Timestamps managed by defaults/triggers. |

### `compound_identifiers`

Provider and internal identifiers attached to compounds.

| Column | Notes |
| --- | --- |
| `compound_identifier_id` | Primary key. |
| `compound_id` | Foreign key to `compounds`. |
| `namespace` | Identifier namespace, for example `a_number`, `unii`, `pubchem_cid`, or `chembl_id`. |
| `identifier_value` | Original identifier value. |
| `normalized_value` | Generated identifier value used for matching and uniqueness. |
| `is_primary` | Marks the preferred identifier for a compound within a namespace. |
| `created_at`, `updated_at` | Timestamps managed by defaults/triggers. |

Constraints:
- Unique `(namespace, normalized_value)`.
- At most one primary identifier per `(compound_id, namespace)`.

### `compound_names`

Preferred names and aliases.

| Column | Notes |
| --- | --- |
| `compound_name_id` | Primary key. |
| `compound_id` | Foreign key to `compounds`. |
| `name` | Original name. |
| `normalized_name` | Generated lowercased/whitespace-normalized value. |
| `name_type` | Usually `preferred` or `alias`. |
| `is_preferred` | Marks the preferred display name. |
| `created_at`, `updated_at` | Timestamps managed by defaults/triggers. |

Constraints:
- Unique `(compound_id, normalized_name)`.
- At most one preferred name per compound.

### `source_records`

Provenance records for manual rows, CSV rows, source measurements, and enrichment assertions.

| Column | Notes |
| --- | --- |
| `source_record_id` | Primary key. |
| `source_name` | Source system or workflow, for example `chembl`, `pubchem`, `manual`, `identifier_csv`, or `unichem`. |
| `source_record_key` | Stable source-specific key. |
| `record_type` | Workflow type, for example `manual_entry`, `csv_import`, `identifier_enrichment`, or `structure_enrichment`. |
| `source_release` | Optional source release/version. |
| `source_url` | Optional source URL. |
| `raw_payload` | JSONB payload with the source row or API response details. |
| `created_at`, `updated_at` | Timestamps managed by defaults/triggers. |

Constraint:
- Unique `(source_name, source_record_key)`.

### `compound_identifier_sources`

Join table recording which `source_records` support attached identifiers.

| Column | Notes |
| --- | --- |
| `compound_identifier_source_id` | Primary key. |
| `compound_identifier_id` | Foreign key to `compound_identifiers`. |
| `source_record_id` | Foreign key to `source_records`. |
| `created_at` | Insert timestamp. |

Constraint:
- Unique `(compound_identifier_id, source_record_id)`.

### `compound_structure_assertions`

Source-backed structure assertions from enrichment workflows.

| Column | Notes |
| --- | --- |
| `compound_structure_assertion_id` | Primary key. |
| `compound_id` | Foreign key to `compounds`. |
| `source_record_id` | Foreign key to `source_records`. |
| `canonical_smiles` | Source-provided canonical SMILES. |
| `standard_inchi` | Source-provided standard InChI. |
| `standard_inchikey` | Source-provided standard InChIKey. |
| `created_at` | Insert timestamp. |

Constraint:
- Unique `(compound_id, source_record_id)`.

### `ic50_results`

Normalized hERG IC50 measurements.

| Column | Notes |
| --- | --- |
| `result_id` | Primary key. |
| `compound_id` | Foreign key to `compounds`. |
| `source_record_id` | Foreign key to `source_records`. |
| `endpoint` | Defaults to `IC50`. |
| `ic50_value` | Positive source value. |
| `ic50_unit` | One of `pM`, `nM`, `uM`, `mM`. |
| `qualifier` | One of `=`, `<`, `>`. |
| `ic50_um` | Generated value converted to micromolar. |
| `pic50` | Generated `6 - log10(ic50_um)` value. |
| `pic50_qualifier` | Generated inverted qualifier because pIC50 increases as IC50 decreases. |
| `created_at`, `updated_at` | Timestamps managed by defaults/triggers. |

Constraint:
- Unique `(source_record_id, endpoint)`.

## Read Views

### `compound_summary_v`

One row per compound with display-friendly identifier and name columns:

`compound_id`, `a_number`, `unii`, `pubchem_cid`, `chembl_id`, `preferred_name`, `common_names`, `canonical_smiles`, `standard_inchi`, `standard_inchikey`, `created_at`, `updated_at`.

### `ic50_result_summary_v`

One row per IC50 result with measurement, provenance, compound identifiers, and a display label:

`result_id`, `compound_id`, `source_record_id`, `endpoint`, `ic50_value`, `ic50_unit`, `qualifier`, `ic50_um`, `pic50`, `pic50_qualifier`, `created_at`, `updated_at`, `source_name`, `source_record_key`, `source_release`, `source_url`, `preferred_name`, `a_number`, `unii`, `pubchem_cid`, `chembl_id`, `standard_inchikey`, `compound_label`.

## Helper Functions

Application code calls these database functions instead of writing directly to every table:

- `register_compound_v2(...)`
- `resolve_compound_id(...)`
- `resolve_compound_by_keys(...)`
- `upsert_source_record(...)`
- `upsert_ic50_result(...)`

Normalization and generated-value helpers:

- `normalize_identifier(namespace, value)`
- `normalize_name(value)`
- `convert_to_um(value, unit)`
- `invert_qualifier(qualifier)`

## Measurement Rules

The source value, unit, and qualifier are preserved in `ic50_results`. PostgreSQL derives:

- `ic50_um`: `pM * 0.000001`, `nM * 0.001`, `uM`, or `mM * 1000`
- `pic50`: `6 - log10(ic50_um)`
- `pic50_qualifier`: `<` becomes `>`, `>` becomes `<`, and `=` stays `=`
