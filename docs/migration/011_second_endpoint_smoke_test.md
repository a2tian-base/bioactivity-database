# Spec 011: Add second endpoint smoke test

## Goal

Prove that the generalized system can support a second endpoint without adding a new result table, new endpoint-specific schema, or new endpoint-specific pipeline.

The preferred smoke-test endpoint is `cyp3a4_ic50` because it exercises a different target while preserving the concentration-potency measurement class.

## Background

The migration is only successful if hERG IC50 is no longer the organizing assumption of the system. A second endpoint should be created by data/configuration plus fixture tests, not by new schema.

For this spec, use CYP3A4 IC50 as the second endpoint unless repository constraints make another endpoint simpler. CYP3A4 is a human cytochrome P450 target commonly used in drug metabolism and inhibition datasets. As of the spec authoring context, useful public identifiers are:

```text
ChEMBL target: CHEMBL340
NCBI Gene: 1576
Gene symbol: CYP3A4
Organism: Homo sapiens
```

If Codex verifies a different current identifier from source documentation, use the verified value and document the source in the PR summary.

## Non-goals

- Do not add new tables.
- Do not add target catalog, assay catalog, measurement ontology, or endpoint_sources.
- Do not add a full endpoint discovery UI.
- Do not require live ChEMBL or PubChem calls in tests.
- Do not implement a non-concentration endpoint yet.
- Do not remove hERG IC50 compatibility.
- Do not change ChEMBL/PubChem adapter semantics beyond supporting the new endpoint config.

## Files likely to change

Likely files:

```text
db/init/001_schema.sql                  # seed endpoint if seed data lives here
app/bioactivity/endpoints.py             # only if endpoint loading assumes herg_ic50
tests/fixtures/cyp3a4_ic50/...
tests/...
docs/migration/...
```

Use the repository's existing seed-data convention.

## Required behavior

After this spec is implemented:

```text
- A second active endpoint, cyp3a4_ic50, exists in the endpoint seed/test data.
- cyp3a4_ic50 uses the same generic endpoint schema as herg_ic50.
- cyp3a4_ic50 can be loaded through the endpoint loader.
- cyp3a4_ic50 ChEMBL source config can construct the ChEMBL adapter.
- A fixture CYP3A4 IC50 source row maps to MeasurementInput.
- The pipeline can write a cyp3a4_ic50 fixture result to bioactivity_results.
- No schema changes are required beyond adding seed data, if seed data is in schema init.
```

## Database changes

No structural database changes.

Add seed/test data for `cyp3a4_ic50` only if endpoint seeds live in the schema init or seed files.

Example endpoint:

```json
{
  "endpoint_key": "cyp3a4_ic50",
  "display_name": "CYP3A4 IC50",
  "spec": {
    "target": {
      "preferred_name": "Cytochrome P450 3A4",
      "gene_symbol": "CYP3A4",
      "organism": "Homo sapiens",
      "identifiers": {
        "chembl_target_id": "CHEMBL340",
        "ncbi_gene_id": "1576"
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
      "target_chembl_id": "CHEMBL340",
      "standard_type": "IC50",
      "standard_relation__in": ["=", "<", ">"],
      "data_validity_comment__isnull": true
    },
    "pubchem": {
      "target_gene_symbol": "CYP3A4",
      "target_gene_id": "1576",
      "activity_name_regex": "(?i)\\bIC50\\b"
    }
  }
}
```

If there is concern about making this a production seed immediately, add it only to test fixtures and document the difference.

## Python/API changes

Only minimal generalization fixes are allowed.

Examples of acceptable changes:

```text
- remove an accidental hard-coded herg_ic50 assumption from endpoint loading
- ensure adapter source config values are not hard-coded to KCNH2/CHEMBL240
- allow tests to pass endpoint_key/source_config into fake pipeline
```

Examples of unacceptable changes:

```text
- adding a new cyp3a4-specific adapter
- adding a new cyp3a4-specific result table
- adding source catalog discovery
- broad parser rewrites unrelated to the fixture
```

## Fixtures to add

Add minimal CYP3A4 IC50 fixtures that match existing adapter input shapes.

Recommended directory:

```text
tests/fixtures/cyp3a4_ic50/
  chembl_activity_ic50_equal.json
  pubchem_concise_ic50_equal.json
```

If PubChem fixture shape is difficult, start with ChEMBL only and document PubChem as a follow-up. The main proof is that the endpoint system is no longer hERG-only.

## Tests required

Minimum tests:

```text
- cyp3a4_ic50 endpoint can be loaded.
- cyp3a4_ic50 measurement.type = IC50.
- cyp3a4_ic50 source_configs.chembl.target_chembl_id = CHEMBL340.
- ChEMBL adapter constructed from cyp3a4_ic50 source config uses CHEMBL340, not CHEMBL240.
- CYP3A4 IC50 ChEMBL fixture maps to MeasurementInput with measurement_type = IC50 and value_kind = concentration.
- Pipeline fake/fixture write creates bioactivity_results row with endpoint_id for cyp3a4_ic50.
- hERG IC50 tests still pass.
- No new result table is required or referenced.
```

Do not call live ChEMBL or PubChem.

## Validation commands

Preferred:

```bash
python -m pytest
```

## Acceptance criteria

- [ ] `cyp3a4_ic50` is represented as an endpoint config/fixture.
- [ ] The second endpoint uses the same generic schema and pipeline as hERG IC50.
- [ ] No new endpoint-specific result table was added.
- [ ] No new endpoint-specific adapter was added.
- [ ] ChEMBL adapter config is target-specific and not hard-coded to hERG.
- [ ] A second-endpoint fixture writes to `bioactivity_results`.
- [ ] hERG IC50 tests continue to pass.

## Notes for Codex

- Treat this as a smoke test, not a full CYP3A4 data-curation project.
- Do not overfit tests to real live ChEMBL/PubChem response payloads beyond the fixture fields the adapters already need.
- If the repository maintainers prefer not to seed CYP3A4 in production init, keep it as a test fixture and explain how production users would add the row.
