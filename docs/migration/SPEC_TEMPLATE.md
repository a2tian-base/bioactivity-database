# Spec <number>: <short title>

## Goal

Describe the single outcome this spec should accomplish. Keep this to one implementation purpose.

Example:

```text
Add the generic endpoint/result/run tables without changing current hERG IC50 ingestion behavior.
```

## Background

Explain the current behavior and why this change is needed. Include relevant file paths and current concepts.

This section should provide enough context that Codex does not need to infer the design from unrelated files.

## Non-goals

List hard scope boundaries. Codex must not implement these items in this PR.

Example:

```text
- Do not refactor ChEMBL or PubChem adapters.
- Do not modify Streamlit UI.
- Do not remove ic50_results.
- Do not add target catalog or measurement ontology tables.
```

## Files likely to change

List expected files or directories. This is not necessarily exhaustive, but it should keep the change bounded.

Example:

```text
- db/init/001_schema.sql
- tests/...
- docs/migration/...
```

## Required behavior

Describe concrete behavior after implementation.

Use precise bullets. Avoid vague instructions like "make it better" or "clean up the code."

Example:

```text
- A fresh database initialization creates the new tables.
- Existing hERG IC50 tables and functions remain available.
- The new endpoints table contains a seeded herg_ic50 row.
```

## Database changes

Describe DDL changes, constraints, indexes, seed data, triggers, compatibility views, and migration-order requirements.

Include explicit table shapes where relevant.

If no database change is required, write:

```text
No database changes.
```

## Python/API changes

Describe model, function, adapter, CLI, or UI changes.

If no Python/API change is required, write:

```text
No Python/API changes.
```

## Tests required

List specific tests Codex must add or update.

Tests should not require live ChEMBL or PubChem network calls. Use fixtures, mocks, local database initialization, or existing test infrastructure.

Example:

```text
- Test that hERG IC50 fixture rows still map to the same normalized values.
- Test that the database creates endpoints, bioactivity_results, and ingestion_runs.
- Test that invalid value_kind is rejected.
```

## Validation commands

List commands Codex should run when possible.

Use repository-specific commands if known. If the repository has no established command, give the preferred command and require Codex to report if it cannot run it.

Example:

```bash
python -m pytest
```

If database validation is required:

```bash
# Use the repository's existing database test or docker-compose workflow if present.
```

## Acceptance criteria

Checklist for PR review.

Example:

```text
- [ ] Existing hERG IC50 tests pass.
- [ ] New tests pass.
- [ ] No deferred tables were added.
- [ ] The PR description lists validation commands run.
```

## Notes for Codex

Implementation hints, pitfalls, edge cases, and preferred approaches.

This section may include guidance such as:

```text
- Inspect existing schema conventions before adding DDL.
- Use existing updated_at trigger patterns if present.
- Prefer small helper functions over broad rewrites.
- Preserve compatibility with current script names.
```
