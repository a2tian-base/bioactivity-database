# AGENTS.md

## Project context

This repository is being migrated from a hERG IC50-specific database into a general endpoint-driven bioactivity ingestion system.

Before implementing any migration task, read:

```text
docs/migration/000_overview.md
```

Then read the specific numbered spec assigned for the task.

Implement only that spec. Do not implement later phases early.

## Core migration model

The target core model is deliberately small:

```text
compounds
compound_identifiers
source_records
endpoints
bioactivity_results
ingestion_runs
```

Existing support tables may remain:

```text
compound_names
compound_structure_assertions
compound_identifier_sources
ic50_results
```

Do not add the following unless a specific later spec explicitly asks for them:

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

Endpoint semantics should initially live in:

```text
endpoints.spec JSONB
endpoints.source_configs JSONB
```

Application code should validate and interpret those JSON specs.

## Non-negotiable guardrails

- Preserve existing hERG IC50 behavior unless the active spec explicitly changes it.
- Do not drop `ic50_results` during the initial migration.
- Do not remove existing scripts during the initial migration; compatibility wrappers come later.
- Do not merge `source_records` into `bioactivity_results`.
- Do not make tests depend on live ChEMBL, PubChem, UniChem, or other network services.
- Do not introduce broad rewrites when a narrow change satisfies the spec.
- Do not add new production dependencies unless the spec explicitly requires them or the repository already uses that dependency pattern.
- Do not silently change scientific normalization semantics.

## Implementation style

Prefer small, reviewable diffs.

For each task:

```text
1. Inspect the relevant existing files before editing.
2. Identify the current conventions for SQL, Python, tests, and scripts.
3. Implement the smallest change that satisfies the current spec.
4. Add or update tests required by the spec.
5. Run validation commands when possible.
6. Break the completed work into small, reviewable git commits.
7. Push the branch and open a pull request in the remote repository.
8. Summarize what changed, what tests ran, and any commands that could not be run.
```

If committing, pushing, or opening a pull request is blocked by the local environment or repository access, state exactly what could not be completed and why.

When a spec has `Non-goals`, treat them as hard boundaries.

If repository conventions conflict with a spec, follow repository conventions where possible and explain the discrepancy in the PR summary.

## Database rules

- Use explicit SQL for schema changes.
- Preserve existing schema conventions, naming style, constraints, and trigger patterns.
- Keep PostgreSQL-specific behavior where the current schema depends on PostgreSQL features such as JSONB, generated columns, and constraints.
- Use `JSONB` for endpoint specs, source configs, assay context, quality flags, run QC summaries, and error summaries during the initial migration.
- Add indexes only when they support expected query paths or constraints.
- Keep raw external provenance in `source_records.raw_payload`.
- Keep normalized measurements in `bioactivity_results`.
- Preserve existing functions and views unless the active spec explicitly changes them.

## Python rules

- Preserve current public behavior of hERG ingestion while introducing generic abstractions.
- Prefer additive changes before replacing old paths.
- Keep ChEMBL and PubChem behavior covered by fixtures or mocks.
- Do not require live external API calls in unit tests.
- Use precise names that distinguish raw source records from normalized measurements.
- Put endpoint-specific variability in endpoint specs, source configs, adapter mapping, and validation code.

## Testing rules

- Add tests for every migration step.
- Prefer deterministic fixtures over live API calls.
- If the repository already has a test framework, follow it.
- If there is no established test framework, prefer `pytest` and keep the setup minimal.
- Run the repository's established validation commands when possible.
- If a command cannot be run in the current environment, say exactly why.

## PR summary expectations

Every PR should summarize:

```text
What changed
Why it changed
Tests added or updated
Validation commands run
Known limitations or follow-up specs
```

The PR should also state whether any active-spec acceptance criteria remain incomplete.
