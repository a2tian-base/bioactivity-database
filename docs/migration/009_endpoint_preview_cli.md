# Spec 009: Add endpoint preview CLI

## Goal

Add a CLI command that previews source rows for a saved endpoint without writing `source_records`, `bioactivity_results`, `ic50_results`, or `ingestion_runs`.

The preview command should help users inspect what an endpoint/source configuration will ingest before running the full pipeline.

## Background

The generalized system stores source-specific query configs in `endpoints.source_configs`. Before a user runs ingestion, they should be able to preview:

```text
example source rows
mapped measurement fields
observed units and relations
skip reasons
basic warnings
```

This spec adds preview behavior after endpoint loading and adapter config refactors are in place.

## Non-goals

- Do not write to the database except for reading the endpoint row.
- Do not modify the ingestion pipeline write path.
- Do not modify Streamlit UI.
- Do not add endpoint discovery or source catalog tables.
- Do not add a second endpoint.
- Do not broaden ChEMBL or PubChem scientific filtering.
- Do not require live external API calls in tests.

## Files likely to change

Likely files:

```text
app/bioactivity/preview.py
app/bioactivity/cli.py
app/herg/sources/chembl.py          # only if a preview-safe iterator hook is needed
app/herg/sources/pubchem.py         # only if a preview-safe iterator hook is needed
tests/...
```

Use the repository's actual CLI/script conventions.

## Required behavior

After this spec is implemented, a user can run a command equivalent to:

```bash
python -m app.bioactivity.preview --endpoint herg_ic50 --source chembl --limit 20
python -m app.bioactivity.preview --endpoint herg_ic50 --source pubchem --limit 20
```

The exact module path may differ, but the command must support:

```text
--endpoint
--source
--limit
```

The preview should:

```text
- load the endpoint by endpoint_key
- load the selected source config
- instantiate the selected adapter from endpoint/source config
- fetch or iterate up to limit raw rows
- attempt to map rows to MeasurementInput
- print accepted examples and skipped examples
- report basic counts: raw rows examined, accepted, skipped, errors
- avoid database writes
```

## Database changes

No database changes.

The command may read `endpoints`. It must not create or modify `source_records`, `bioactivity_results`, `ic50_results`, or `ingestion_runs`.

## Python/API changes

### 1. Add preview service

Add a function equivalent to:

```python
def preview_endpoint_source(
    conn,
    *,
    endpoint_key: str,
    source_name: str,
    limit: int = 20,
) -> PreviewResult:
    ...
```

`PreviewResult` should contain enough structured data for tests and CLI formatting:

```text
endpoint_key
source_name
raw_rows_examined
accepted_count
skipped_count
error_count
accepted_examples
skipped_examples
warnings
```

### 2. Add CLI formatter

Print a human-readable preview. Keep it simple.

Suggested output sections:

```text
Endpoint
Source
Query config
Summary
Accepted examples
Skipped examples
Warnings
```

Do not expose huge raw payloads by default. Truncate or summarize raw rows.

### 3. Adapter preview hooks

If existing adapters only support full ingestion iteration, add a safe way to limit row fetching.

Do not duplicate adapter logic in the CLI. Preview should use the same row mapping logic that ingestion uses.

### 4. Error handling

If an unsupported source is requested, return a clear error.

If an endpoint lacks the requested source config, return a clear error.

If a row fails mapping, capture the error in preview output rather than crashing the whole preview unless the error is setup/configuration-level.

## Tests required

Use fake adapters or monkeypatches. Do not call live ChEMBL or PubChem.

Minimum tests:

```text
- Preview loads herg_ic50 and chembl config.
- Preview with fake adapter returns accepted_count and skipped_count.
- Preview honors --limit.
- Preview does not insert source_records.
- Preview does not insert bioactivity_results.
- Preview does not insert ingestion_runs.
- Unsupported source produces clear error.
- Missing source config produces clear error.
- CLI formatter includes endpoint, source, summary, and examples.
```

If testing CLI invocation directly is difficult, test the preview service and a small formatting function.

## Validation commands

Preferred:

```bash
python -m pytest
```

Optionally run a local help command:

```bash
python -m app.bioactivity.preview --help
```

Use the actual module path implemented in the repository.

## Acceptance criteria

- [ ] Preview CLI or script exists.
- [ ] Preview supports endpoint, source, and limit options.
- [ ] Preview uses endpoint source configs.
- [ ] Preview uses adapter mapping logic.
- [ ] Preview performs no writes to result/provenance/run tables.
- [ ] Preview reports accepted and skipped rows.
- [ ] Tests do not call live external APIs.

## Notes for Codex

- Keep preview output practical, not exhaustive.
- Do not create a local source catalog.
- Do not make preview a prerequisite for ingestion in this spec; it is a user-facing inspection tool.
- If current adapters need network access for real preview, tests should still use fake adapters.
