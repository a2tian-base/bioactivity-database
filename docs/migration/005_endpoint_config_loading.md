# Spec 005: Add endpoint configuration loading and validation

## Goal

Add application code to load saved endpoint definitions from the `endpoints` table and expose validated source-specific configs to later ingestion specs.

This spec should not change source adapter behavior yet. It only introduces endpoint loading and validation.

## Background

Spec 002 adds an `endpoints` table with:

```text
endpoint_key
display_name
spec JSONB
source_configs JSONB
spec_hash
active
```

The migration stores endpoint semantics in `endpoints.spec` and source-specific query parameters in `endpoints.source_configs`. Later specs will pass those configs into ChEMBL and PubChem adapters.

This spec creates the application-layer bridge from the database row to typed Python objects/helpers.

## Non-goals

- Do not refactor ChEMBL or PubChem adapters.
- Do not modify ingestion pipeline behavior.
- Do not write to `bioactivity_results` from the pipeline.
- Do not modify Streamlit UI.
- Do not add endpoint discovery.
- Do not add `endpoint_sources`, `measurement_types`, target catalog, or source catalog tables.
- Do not make live external API calls.

## Files likely to change

Codex should inspect current DB access conventions first.

Likely files:

```text
app/bioactivity/endpoints.py
app/bioactivity/models.py
app/bioactivity/db.py
app/herg/...                  # only if shared DB utilities live here
tests/...
```

## Required behavior

After this spec is implemented:

```text
- Code can load an active endpoint by endpoint_key.
- Loading a missing endpoint raises a clear error.
- Loading an inactive endpoint raises a clear error unless explicitly allowed by caller.
- Loaded endpoint exposes spec and source_configs as validated dictionaries/objects.
- Code can retrieve a source config by source name, e.g. chembl or pubchem.
- Missing source config produces a clear error.
- The seeded herg_ic50 endpoint can be loaded and validated.
```

## Database changes

No database changes.

## Python/API changes

### 1. Add endpoint model/helper

Add an application-level object equivalent to:

```python
@dataclass(frozen=True)
class EndpointConfig:
    endpoint_id: int
    endpoint_key: str
    display_name: str
    spec: dict[str, Any]
    source_configs: dict[str, dict[str, Any]]
    spec_hash: str
    active: bool
```

Use repository conventions if dataclasses are not appropriate.

### 2. Add loader

Add a function equivalent to:

```python
def load_endpoint(conn, endpoint_key: str, *, include_inactive: bool = False) -> EndpointConfig:
    ...
```

Behavior:

```text
- endpoint_key is required and trimmed/validated.
- If no endpoint exists, raise a domain-specific error or ValueError with a clear message.
- If endpoint is inactive and include_inactive is false, raise a clear error.
- Return endpoint_id and parsed JSON fields.
```

### 3. Add source config accessor

Add a function or method equivalent to:

```python
def get_source_config(endpoint: EndpointConfig, source_name: str) -> dict[str, Any]:
    ...
```

Behavior:

```text
- source_name is normalized consistently with existing source names.
- missing source config raises a clear error.
- returned config is a plain dictionary.
```

### 4. Validate minimal endpoint structure

Add lightweight validation that checks:

```text
spec is a dict
source_configs is a dict
spec.measurement.type is present
spec.measurement.value_kind is present
spec.measurement.value_kind is one of allowed value kinds
source_configs values are objects/dicts
```

Do not build a full endpoint ontology.

### 5. Optional CLI helper

If the repository already has a CLI pattern, Codex may add a small endpoint inspection command such as:

```bash
python -m app.bioactivity.endpoints show herg_ic50
```

This is optional. Do not create a large CLI framework in this spec.

## Tests required

Use database-backed tests if the repository has a PostgreSQL test setup.

Minimum tests:

```text
- load_endpoint("herg_ic50") returns endpoint_key = herg_ic50.
- loaded herg_ic50 has measurement.type = IC50.
- loaded herg_ic50 has measurement.value_kind = concentration.
- get_source_config(endpoint, "chembl") returns target_chembl_id = CHEMBL240.
- get_source_config(endpoint, "pubchem") returns target_gene_id = 3757 or "3757".
- loading unknown endpoint raises clear error.
- loading endpoint with invalid measurement.value_kind raises validation error.
- requesting missing source config raises clear error.
```

If modifying DB fixtures is difficult, tests may instantiate endpoint rows through SQL setup and then call the loader.

Do not call ChEMBL or PubChem.

## Validation commands

Preferred:

```bash
python -m pytest
```

## Acceptance criteria

- [ ] Endpoint loading helper exists.
- [ ] Source config accessor exists.
- [ ] `herg_ic50` can be loaded from the database.
- [ ] Basic endpoint spec validation exists.
- [ ] Missing endpoint and missing source config errors are clear.
- [ ] Tests cover successful and failing loads.
- [ ] No adapter or pipeline behavior changed.

## Notes for Codex

- Keep validation intentionally minimal; this is not an ontology layer.
- Preserve source config keys from Spec 002 exactly, because later adapter specs will depend on them.
- Do not add a separate `endpoint_sources` table.
- Do not cache endpoint configs globally unless the repository already has a clear cache pattern.
