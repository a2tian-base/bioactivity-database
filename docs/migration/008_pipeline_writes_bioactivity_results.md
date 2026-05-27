# Spec 008: Write pipeline outputs to bioactivity_results with ingestion runs

## Goal

Update the ingestion pipeline so hERG IC50 ingestion writes generic `bioactivity_results` records and records `ingestion_runs`, while preserving existing hERG IC50 behavior.

This is the activation step for the generic database path.

## Background

Earlier specs added:

```text
endpoints
bioactivity_results
ingestion_runs
MeasurementInput
endpoint loading
adapter config loading
generic upsert_bioactivity_result
```

ChEMBL and PubChem adapters can now be configured from endpoint source configs and can produce generic `MeasurementInput` values for hERG IC50 rows.

This spec connects those pieces in the pipeline.

## Non-goals

- Do not remove `ic50_results`.
- Do not drop or rewrite current hERG scripts.
- Do not modify Streamlit UI.
- Do not add endpoint preview CLI yet.
- Do not add a second endpoint.
- Do not broaden ChEMBL or PubChem scientific filtering.
- Do not add deferred catalog/ontology tables.
- Do not require live external API calls in tests.

## Files likely to change

Likely files:

```text
app/herg/pipeline.py
app/herg/scripts/ingest_chembl_herg.py
app/herg/scripts/ingest_pubchem_herg.py
app/bioactivity/results.py
app/bioactivity/endpoints.py
app/bioactivity/runs.py
tests/...
```

Use the repository's actual paths and conventions.

## Required behavior

After this spec is implemented:

```text
- The pipeline loads endpoint herg_ic50 when running current hERG ingestion scripts.
- The pipeline creates an ingestion_runs row for each source ingestion run.
- The pipeline updates ingestion_runs status and row counters.
- For each accepted source row, the pipeline upserts source_records as before.
- For each accepted source row, the pipeline upserts bioactivity_results using MeasurementInput.
- Each bioactivity_results row links to endpoint_id, compound_id, source_record_id, and ingestion_run_id.
- Existing hERG IC50 output remains available through ic50_results or current read paths.
- Existing hERG scripts remain runnable under their current names.
```

## Database changes

No new tables.

If needed, Codex may add a compatibility view that exposes `bioactivity_results` rows in an IC50-shaped form, but only if that is necessary to preserve current read behavior. Prefer dual-write in this spec if it is simpler and safer.

### Recommended transition strategy

Use temporary dual-write unless the repository structure makes a compatibility view clearly safer:

```text
1. Continue writing to ic50_results exactly as before.
2. Also write equivalent rows to bioactivity_results.
3. Add tests proving the two representations agree for hERG IC50 fixtures.
```

Do not remove direct writes to `ic50_results` in this spec.

## Python/API changes

### 1. Create ingestion run helpers

Add helpers equivalent to:

```python
def start_ingestion_run(conn, *, endpoint_id: int, source_name: str, source_release: str | None, query_config: dict) -> int:
    ...

def finish_ingestion_run(conn, *, ingestion_run_id: int, status: str, counters: dict, qc_summary: dict | None = None, error_summary: dict | None = None) -> None:
    ...
```

Use existing transaction patterns.

The run should start with `status = 'running'` and finish with one of:

```text
succeeded
failed
partial
```

### 2. Load endpoint and source config

Current hERG scripts should resolve:

```text
endpoint_key = herg_ic50
source_name = chembl or pubchem
source_config = endpoint.source_configs[source_name]
```

Then pass the source config into the adapter.

### 3. Write generic results

When a row is successfully mapped and compound/source record upserts succeed, convert to `MeasurementInput` and call `upsert_bioactivity_result`.

The `result_key` should be stable and source-specific:

```text
ChEMBL: prefer activity ID or existing source record key component.
PubChem: prefer AID/SID/CID/activity name/value field combination or existing source record key component.
```

Do not use a random UUID as `result_key`.

### 4. Preserve current skip/error behavior

Current pipeline skip/error behavior should remain stable. Add QC counters where possible, but do not change which hERG rows are accepted or skipped unless a baseline test proves the current behavior is buggy and the PR explicitly documents the fix.

### 5. Preserve current scripts

Existing commands such as hERG ChEMBL/PubChem ingestion should still work.

If adding a generic internal command improves implementation, old scripts may call it, but script names must remain available.

## Tests required

Use fixtures and fake adapters. Do not call live ChEMBL or PubChem.

Minimum tests:

```text
- Running a fake successful ChEMBL-style pipeline creates one ingestion_runs row.
- Accepted fixture row creates/updates a bioactivity_results row.
- bioactivity_results row has endpoint_key = herg_ic50 via endpoint_id.
- bioactivity_results row links to compound_id and source_record_id.
- bioactivity_results row links to ingestion_run_id.
- Running same fixture twice is idempotent for bioactivity_results.
- Existing ic50_results behavior remains available through dual-write or current path.
- Failed/skipped row increments rows_skipped or rows_failed according to existing semantics.
- ingestion_runs status becomes succeeded for all-good run.
- ingestion_runs status becomes failed or partial when the fake adapter raises after partial processing, according to chosen policy.
```

If existing pipeline tests already cover many of these behaviors, update them rather than duplicating unnecessarily.

## Validation commands

Preferred:

```bash
python -m pytest
```

If database integration tests require Docker/PostgreSQL, use the repository's established workflow and document any commands that could not be run.

## Acceptance criteria

- [ ] hERG ingestion pipeline loads `herg_ic50` endpoint.
- [ ] Pipeline creates and finalizes `ingestion_runs` records.
- [ ] Accepted rows write to `bioactivity_results`.
- [ ] Existing hERG IC50 behavior remains available.
- [ ] Existing hERG scripts remain available.
- [ ] Duplicate ingestion is idempotent for generic results.
- [ ] Tests use fixtures/fakes, not live APIs.
- [ ] No UI changes were made.
- [ ] No second endpoint was added.

## Notes for Codex

- This is a high-risk spec. Keep the diff focused on connecting already-added components.
- Prefer dual-write for safety unless a compatibility view is clearly simpler.
- Be explicit in the PR summary about whether the implementation uses dual-write or a compatibility view.
- Do not change scientific normalization or skip semantics while moving the write path.
