# Migration Plan: UI-Based Source Preview and Automatic Ingestion

## Purpose

This migration adds the missing user-facing ingestion loop to the bioactivity database UI.

The current migration target already supports endpoint-aware storage, endpoint configs, preview logic, ingestion runs, and generic bioactivity results. However, the Streamlit UI does not yet let a user preview external source rows or launch automatic ingestion from the browser. Users still need to run ChEMBL and PubChem ingestion from the command line.

The goal of this migration is to add a simple, synchronous Streamlit ingestion workflow that lets a user:

1. Select a saved endpoint.
2. Select a configured external source, such as ChEMBL or PubChem.
3. Preview source rows before writing anything.
4. Run a dry-run ingestion.
5. Run a limited write ingestion after explicit confirmation.
6. Browse newly ingested endpoint-specific results.

This should reuse the existing backend ingestion and preview logic. It should not introduce a job queue, worker service, scheduler, or new database model.

## Current State

The migration has already moved toward an endpoint-driven model:

- `endpoints` stores saved endpoint definitions and source-specific configs.
- `bioactivity_results` stores normalized endpoint measurements.
- `ingestion_runs` stores ingestion execution history and QC summaries.
- Source adapters have been refactored toward endpoint/source-config-driven behavior.
- The preview service can inspect source rows without writing records.
- The Streamlit UI has endpoint-aware browsing and manual-entry behavior.

The missing functionality is UI access to the automatic ingestion path.

## Proposed Solution

Add one new Streamlit tab:

```text
Ingest
```

The tab should provide two related workflows:

```text
Preview source rows
Run ingestion
```

The implementation should be deliberately simple:

```text
Streamlit button click
  -> call preview or ingestion helper synchronously
  -> display final summary in the UI
```

Do not add asynchronous infrastructure yet. A synchronous implementation is sufficient for the first UI-based ingestion workflow, provided the UI defaults to dry-run mode and record limits.

## Non-Goals

This migration must not:

- Add Celery, Redis, RQ, a background worker, scheduler, or async job queue.
- Add new database tables.
- Add endpoint discovery.
- Change ChEMBL or PubChem query semantics.
- Remove existing command-line ingestion scripts.
- Remove legacy hERG IC50 compatibility behavior.
- Perform live ChEMBL or PubChem requests in unit tests.
- Attempt full cancellation, log streaming, or multi-user job management.

Those features can be considered after the simple UI workflow is working.

## Target User Flow

### 1. Select endpoint

The user opens the Streamlit app and selects an existing endpoint, for example:

```text
hERG IC50
```

The selected endpoint determines the available source configs.

### 2. Open the Ingest tab

The user opens:

```text
Ingest
```

The tab shows available sources from the selected endpoint's `source_configs`.

Example:

```text
Source: chembl
Source: pubchem
```

### 3. Preview source rows

The user selects a source and clicks:

```text
Preview source rows
```

The UI calls the existing preview service and displays:

- query config
- source rows examined
- accepted examples
- skipped examples
- error examples
- warning summaries
- observed units and relations, if available

No records are written during preview.

### 4. Run dry-run ingestion

The user leaves dry-run enabled and clicks:

```text
Run ingestion
```

The UI calls the same pipeline used by CLI ingestion, but with `dry_run = true`.

The UI displays final counters, such as:

```text
processed
stored
updated
skipped_invalid
failed
warnings
duration_seconds
```

No database writes should occur for dry-run ingestion except any intentionally recorded dry-run metadata if the existing backend already supports it.

### 5. Run write ingestion

To write records, the user disables dry-run and must check an explicit confirmation checkbox:

```text
I understand this will write records to the database.
```

Then the user clicks:

```text
Run ingestion
```

The UI calls the existing ingestion pipeline. Accepted rows are written through the current backend path, including:

- `source_records`
- `bioactivity_results`
- `ingestion_runs`
- any retained legacy compatibility writes, such as `ic50_results`, if still active

### 6. Browse results

After ingestion completes, the user can go to the endpoint-aware browse/dashboard UI and view newly ingested records.

## Implementation Plan

### Step 1: Add a shared source adapter factory

Create:

```text
app/bioactivity/source_adapters.py
```

Purpose:

```text
Construct source adapters from an endpoint and one source-specific config.
```

Conceptual API:

```python
def build_source_adapter(
    *,
    endpoint,
    source_name: str,
    source_config: dict,
    http_config,
):
    ...
```

Required behavior:

- Support `chembl`.
- Support `pubchem`.
- Normalize source names to lower case.
- Raise a clear `ValueError` for unsupported sources.
- Reuse existing `from_source_config` constructors if present.

This factory should be used by both preview and UI ingestion so adapter construction does not drift.

### Step 2: Refactor preview to use the shared adapter factory

Update the existing preview service so it imports `build_source_adapter` instead of maintaining its own adapter construction map.

Required behavior:

- Existing preview CLI behavior must remain unchanged.
- Preview should still be read-only.
- Existing preview tests should continue to pass.

### Step 3: Add a UI ingestion wrapper

Create:

```text
app/bioactivity/ui_ingestion.py
```

Purpose:

```text
Bridge Streamlit controls to the existing backend ingestion pipeline.
```

Suggested objects:

```python
@dataclass(frozen=True)
class UiIngestionRequest:
    endpoint_key: str
    source_name: str
    dry_run: bool
    max_records: int | None
    commit_every: int
    fail_fast: bool
    request_timeout_seconds: int
    http_retries: int


@dataclass(frozen=True)
class UiIngestionResult:
    endpoint_key: str
    source_name: str
    dry_run: bool
    processed: int
    stored: int
    updated: int
    skipped_invalid: int
    failed: int
    warnings: int
    duration_seconds: float
    ingestion_run_id: int | None = None
```

Suggested function:

```python
def run_ui_ingestion(request: UiIngestionRequest) -> UiIngestionResult:
    ...
```

Required behavior:

1. Load the selected endpoint from the database.
2. Read the selected source config from `endpoint.source_configs`.
3. Construct HTTP/run configuration from the UI request.
4. Build the appropriate source adapter.
5. Call the existing ingestion pipeline.
6. Return a compact result object suitable for Streamlit display.

This module should not contain Streamlit imports. It should be testable independently from the UI.

### Step 4: Add the Streamlit Ingest tab

Update:

```text
app/app.py
```

Add a new tab:

```text
Ingest
```

Suggested function:

```python
def render_ingest_tab(selected_endpoint) -> None:
    ...
```

The tab should:

- Use the currently selected endpoint.
- Read available sources from `selected_endpoint.source_configs`.
- Show an informative message if no sources are configured.
- Provide a source selector.
- Provide preview controls.
- Provide ingestion controls.
- Display preview results and ingestion summaries.

### Step 5: Add preview controls

UI controls:

```text
Source
Preview limit
Preview source rows button
```

Defaults:

```text
Preview limit: 20
Maximum preview limit: 200
```

On click:

```python
preview_endpoint_source(
    conn,
    endpoint_key=selected_endpoint.endpoint_key,
    source_name=source_name,
    limit=preview_limit,
)
```

Display:

- summary counters
- source query config
- accepted examples
- skipped examples
- errors
- warnings

Use compact dataframes where helpful.

### Step 6: Add ingestion controls

UI controls:

```text
Dry run
Limit records
Max records
Commit every
Fail fast
Request timeout seconds
HTTP retries
Confirmation checkbox for write ingestion
Run ingestion button
```

Recommended defaults:

```text
dry_run: true
limit_records: true
max_records: 100
commit_every: 500
fail_fast: false
request_timeout_seconds: 45
http_retries: 4
```

Write ingestion must require confirmation when `dry_run` is false.

Example confirmation text:

```text
I understand this will write records to the database.
```

The Run button should be disabled unless either:

```text
dry_run = true
```

or:

```text
confirmation checkbox = checked
```

### Step 7: Display ingestion results

After ingestion completes, display a compact result summary.

Suggested metrics:

```text
processed
stored
updated
skipped_invalid
failed
warnings
duration_seconds
ingestion_run_id
```

Suggested status behavior:

```text
If dry_run is true:
  show informational dry-run completion message.

If failed > 0:
  show warning message.

If dry_run is false and failed == 0:
  show success message.
```

Also show:

```text
Refresh Browse Results to see newly ingested records.
```

### Step 8: Add tests

Add tests that do not require live network access.

Required tests:

1. Source adapter factory supports `chembl` and `pubchem`.
2. Source adapter factory rejects unsupported sources with a clear error.
3. `run_ui_ingestion` constructs the expected endpoint/source/run configuration using mocks.
4. `run_ui_ingestion` calls the existing pipeline with the selected endpoint key and source config.
5. Streamlit app remains import-safe.
6. Preview rendering can handle a fake preview result without network calls.
7. Existing CLI preview and ingestion tests continue to pass.

Use mocks for:

- endpoint loading
- adapter construction
- pipeline execution
- preview service results

### Step 9: Update documentation

Update the README or migration docs to describe UI ingestion.

Minimum documentation:

```text
How to preview rows from the UI.
How to run dry-run ingestion from the UI.
How to run limited write ingestion from the UI.
Why dry-run and max-record limits are recommended.
Where to inspect ingestion run results.
```

## Suggested Codex Spec

Create:

```text
docs/migration/013_streamlit_source_ingestion.md
```

Suggested title:

```text
# Spec 013: Add Streamlit source preview and ingestion controls
```

The spec should instruct Codex to implement the steps above as a single, bounded PR.

## Acceptance Criteria

This migration is complete when:

1. The Streamlit UI has an `Ingest` tab.
2. The tab lists sources configured for the selected endpoint.
3. The user can preview ChEMBL or PubChem rows for `herg_ic50` from the UI.
4. Preview does not write database records.
5. The user can run dry-run ingestion from the UI.
6. The user can run limited write ingestion from the UI after confirmation.
7. Write ingestion uses the existing backend pipeline.
8. Write ingestion updates `ingestion_runs` and `bioactivity_results` through the existing backend path.
9. Existing CLI ingestion scripts still work.
10. Existing endpoint-aware browsing still works.
11. Tests do not make live ChEMBL or PubChem network calls.
12. No new database tables are added.

## Risks and Mitigations

### Risk: Long-running UI request

Large source ingestion may take longer than is comfortable for a synchronous Streamlit request.

Mitigation:

- Default to dry-run.
- Default to `max_records = 100`.
- Allow users to increase the limit deliberately.
- Keep larger production-scale runs on the CLI until a background job system exists.

### Risk: Accidental database writes

A user could unintentionally run write ingestion.

Mitigation:

- Dry-run defaults to true.
- Write mode requires an explicit confirmation checkbox.
- Display the selected endpoint and source prominently.
- Display query config before ingestion.

### Risk: Divergent adapter construction

Preview and ingestion could instantiate source adapters differently.

Mitigation:

- Add one shared source adapter factory.
- Use it in both preview and UI ingestion.

### Risk: Tests become slow or flaky

Live source requests would make tests unreliable.

Mitigation:

- Mock preview, adapter construction, and pipeline execution.
- Keep live-source checks as optional manual smoke tests only.

## Deferred Enhancements

Do not implement these in this migration:

```text
background ingestion jobs
job cancellation
progress log streaming
ingestion scheduling
multi-user job ownership
source discovery UI
endpoint creation UI
confidence scoring
full source catalog synchronization
```

These can be added later if UI ingestion becomes a central workflow.
