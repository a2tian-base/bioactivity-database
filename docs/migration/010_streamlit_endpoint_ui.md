# Spec 010: Update Streamlit UI for endpoint-aware browsing and entry

## Goal

Update the Streamlit UI from a hERG IC50-specific interface to an endpoint-aware bioactivity interface, while keeping hERG IC50 usable.

This spec should focus on UI integration with the new endpoint/result model. It should not change ingestion semantics.

## Background

The current UI is titled around hERG IC50 and exposes IC50-specific tabs and forms. The migration target stores saved endpoints in `endpoints` and normalized measurements in `bioactivity_results`.

The UI should allow users to select an endpoint before browsing results, manually adding measurements, or exporting data.

## Non-goals

- Do not change database schema.
- Do not refactor ChEMBL or PubChem adapters.
- Do not change ingestion pipeline behavior.
- Do not remove old ingestion scripts.
- Do not add endpoint discovery or source catalog UI.
- Do not add a full endpoint-builder wizard.
- Do not add complex support for every endpoint class in manual entry.
- Do not drop the existing hERG IC50 workflow.

## Files likely to change

Likely files:

```text
app/app.py
app/bioactivity/endpoints.py
app/bioactivity/results.py
app/bioactivity/ui_helpers.py
tests/...
```

Use repository conventions for Streamlit support code.

## Required behavior

After this spec is implemented:

```text
- UI title is generalized, e.g. Bioactivity Database.
- UI can list active endpoints from the endpoints table.
- User can select an endpoint, with herg_ic50 available by default.
- Browse/results view filters by selected endpoint_id.
- Result display uses bioactivity_results when available.
- hERG IC50 result display remains understandable, including IC50 and pIC50 fields when present.
- Manual entry is endpoint-aware for concentration endpoints at minimum.
- CSV upload is either endpoint-aware for concentration endpoints or clearly limited with a UI message.
```

## Database changes

No database changes.

## Python/API changes

### 1. Endpoint selection helper

Add or reuse a helper to list active endpoints:

```python
def list_active_endpoints(conn) -> list[EndpointConfig]:
    ...
```

The UI should display `display_name` and use `endpoint_id`/`endpoint_key` internally.

### 2. Result query helper

Add or reuse a helper to query `bioactivity_results` by endpoint.

Suggested output fields for UI:

```text
compound_id
compound identifiers or display name if available
source_name
measurement_type
value_kind
standard_value
standard_unit
standard_relation
p_value
p_value_relation
value_text
assay_context summary
quality_flags summary
source_record_id
created_at
updated_at
```

Join to `source_records` for source name and source URL if useful.

### 3. hERG IC50 compatibility display

For concentration IC50 rows, label fields clearly:

```text
IC50, standardized unit
pIC50, if present
relation/qualifier
source
```

Do not require every endpoint to have p-values.

### 4. Manual entry

At minimum, support manual entry for `value_kind = concentration` endpoints.

The form should use endpoint spec values where possible:

```text
measurement.type
measurement.canonical_unit
normalization.allowed_units
normalization.allowed_relations
```

If the selected endpoint has an unsupported `value_kind`, show a clear message rather than rendering a wrong form.

### 5. CSV upload

For this spec, CSV upload may be limited to concentration endpoints. If so, the UI must state that limitation clearly.

Do not silently treat all endpoints as IC50.

## Tests required

Streamlit apps are often hard to test end-to-end. Add testable helper functions and smoke tests.

Minimum tests:

```text
- list_active_endpoints returns herg_ic50.
- result query helper filters by endpoint_id.
- result formatting handles concentration rows with p_value.
- result formatting handles categorical/text rows without p_value, if helper supports them.
- manual-entry schema helper returns concentration fields for herg_ic50.
- unsupported manual-entry value_kind returns a clear unsupported-state object/message.
- app module imports without starting Streamlit execution side effects that break tests.
```

Do not rely on live ChEMBL/PubChem.

## Validation commands

Preferred:

```bash
python -m pytest
```

Optionally run a local import check:

```bash
python -c "import app.app"
```

If Streamlit is available in the environment, Codex may also run the repository's normal Streamlit smoke command, but do not require manual browser testing as the only validation.

## Acceptance criteria

- [ ] UI title and labels are generalized.
- [ ] Active endpoint selector exists.
- [ ] Browse/results view filters by selected endpoint.
- [ ] hERG IC50 remains usable and understandable.
- [ ] UI does not assume every endpoint has IC50 or pIC50.
- [ ] Manual entry is endpoint-aware for concentration endpoints or clearly limited.
- [ ] Tests cover helper logic.
- [ ] No ingestion semantics changed.

## Notes for Codex

- Keep UI changes incremental. Do not redesign the whole app.
- Prefer adding helper functions that can be unit tested outside Streamlit.
- Avoid hiding missing generic-data support by falling back silently to `ic50_results` without indicating the selected endpoint.
- If both `ic50_results` and `bioactivity_results` exist during transition, make the data source explicit in code comments.
