# Bioactivity Database

This repository contains a small SQL-first application for storing, enriching, and browsing endpoint-driven bioactivity measurements. It uses PostgreSQL for the canonical data model, Streamlit for manual entry and browsing, and Python pipeline scripts for ChEMBL, PubChem, UniChem, identifier, and structure enrichment workflows.

The primary result store is `bioactivity_results`, keyed by saved endpoint definitions such as `herg_ic50` and `cyp3a4_ic50`. The legacy `ic50_results` table is retained for hERG IC50 compatibility and is still dual-written by IC50 ingestion paths so existing consumers are not broken.

## Quickstart

Prerequisites:
- Docker with the Docker Compose plugin

Create a local environment file:

```powershell
Copy-Item .env.example .env
```

For local use, keep `APP_DOMAIN=localhost` and set a non-default `POSTGRES_PASSWORD` if the database will contain real data.

Start the stack:

```powershell
docker compose up -d --build
```

Open the app:
- Local Streamlit UI: `http://localhost:8501`
- Domain deployment through Caddy: `https://<APP_DOMAIN>`

Check service status:

```powershell
docker compose ps
```

Open a database shell:

```powershell
docker compose exec db sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Stop the stack:

```powershell
docker compose down
```

Reset all local data and rebuild from `db/init/001_schema.sql`:

```powershell
docker compose down -v
docker compose up -d --build
```

## Repository Map

```text
.
|-- app/                  Streamlit app, Python package, and pipeline scripts
|-- db/init/              PostgreSQL schema loaded into fresh Docker volumes
|-- deploy/Caddyfile      Local/domain reverse proxy config
|-- tests/                Unit, adapter, and optional DB integration tests
|-- docker-compose.yml    Local PostgreSQL + Streamlit + Caddy stack
|-- requirements-dev.txt  Test-only Python dependencies
`-- docs/                 Handoff and reference documentation
```

## Common Workflows

Use the Streamlit UI for manual entry, CSV upload, endpoint-filtered browsing, source ingestion, dashboard views, and CSV export. The upload tab includes CSV templates for compounds and hERG IC50-compatible result rows.

The `Ingest` tab lets users preview configured ChEMBL or PubChem rows for the selected endpoint, run dry-run ingestion, and run limited write ingestion after explicit confirmation. Keep dry-run and max-record limits enabled for first passes; larger production-scale runs can still use the CLI.

Generic endpoint ingestion is the preferred source ingestion path. Run scripts from the `frontend` container:

```powershell
docker compose exec frontend python /app/scripts/ingest_endpoint.py --endpoint-key herg_ic50 --source chembl --dry-run --max-records 100
docker compose exec frontend python /app/scripts/ingest_endpoint.py --endpoint-key cyp3a4_ic50 --source chembl --dry-run --max-records 100
docker compose exec frontend python /app/scripts/ingest_endpoint.py --endpoint-key herg_ic50 --source pubchem --dry-run --max-records 100
```

The old hERG script names remain available as compatibility wrappers around the same endpoint ingestion path:

```powershell
docker compose exec frontend python /app/scripts/ingest_chembl_herg.py --dry-run --max-records 100
docker compose exec frontend python /app/scripts/ingest_pubchem_herg.py --dry-run --max-records 100
docker compose exec frontend python /app/scripts/enrich_identifiers_from_unichem.py --dry-run --max-records 100
docker compose exec frontend python /app/scripts/enrich_structures.py --dry-run --max-records 100
```

Remove `--dry-run` when the output looks correct. Use `--help` on any script for provider-specific flags and output paths.

## Tests

Install dependencies in a local Python environment:

```powershell
pip install -r app/requirements.txt -r requirements-dev.txt
```

Run unit and adapter tests:

```powershell
python -m pytest -q
```

Run DB integration tests against the configured database:

```powershell
$env:HERG_TEST_DB = "1"
python -m pytest -q tests/integration
```

## Documentation

- [Data model](docs/data-model.md): database tables, generated fields, views, and helper functions.
- [Operations](docs/operations.md): ingestion/enrichment commands, CSV formats, validation, and deployment notes.

## Notes for Maintainers

- `db/init/*.sql` runs only when PostgreSQL initializes a fresh data volume. After schema changes, reset the volume or apply migrations manually.
- Use `compound_summary_v` and `bioactivity_results` for endpoint-aware read access. `ic50_result_summary_v` remains available for legacy hERG IC50 reads.
- Keep identifiers in `compound_identifiers`; do not add provider-specific identifier columns to `compounds`.
- The database and Streamlit ports are bound to localhost in Compose. Public access should go through Caddy or another reverse proxy.
