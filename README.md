# Bioactivity Database

Small Streamlit and PostgreSQL application for storing, ingesting, browsing, and exporting endpoint-driven bioactivity measurements.

The project started as a hERG IC50 database and now stores normalized measurements in `bioactivity_results` for saved endpoint definitions such as `herg_ic50` and `cyp3a4_ic50`. The legacy `ic50_results` table remains for hERG IC50 compatibility.

## Quickstart

Prerequisite: Docker with the Docker Compose plugin.

```bash
cp .env.example .env
docker compose up -d --build
```

Open the Streamlit app at `http://localhost:8501`.

Useful commands:

```bash
docker compose ps
docker compose logs -f frontend
docker compose exec db sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
docker compose down
```

To reset local data and reload the schema from `db/init/001_schema.sql`:

```bash
docker compose down -v
docker compose up -d --build
```

## Deploy

For a single VM deployment:

1. Install Docker.
2. Copy this repository to the server.
3. Create `.env` from `.env.example`.
4. Set `POSTGRES_PASSWORD`, `APP_DOMAIN`, `HTTP_PORT`, and `HTTPS_PORT`.
5. Run `docker compose up -d --build`.

The Compose stack runs PostgreSQL, Streamlit, and Caddy. Local database and UI ports bind to `127.0.0.1`; public traffic should enter through Caddy or another reverse proxy.

For managed PostgreSQL, run `db/init/001_schema.sql` against the database, deploy the `app/` container, and set `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, and `DB_PASSWORD`.

## Use

In the Streamlit app:

- `Find Endpoint`: search saved endpoints and source-backed candidates.
- `Add Compound`: create or update compound identity records.
- `Add Measurement`: add a normalized measurement for the selected endpoint.
- `Upload CSV`: import compounds and hERG IC50-compatible result rows.
- `Ingest`: preview ChEMBL or PubChem source rows, then run dry-run or confirmed write ingestion.
- `Browse Results` and `Dashboard`: inspect and export endpoint-filtered normalized results.

Generic source ingestion can also run from the app container:

```bash
docker compose exec frontend python /app/scripts/ingest_endpoint.py --endpoint-key herg_ic50 --source chembl --dry-run --max-records 100
docker compose exec frontend python /app/scripts/ingest_endpoint.py --endpoint-key cyp3a4_ic50 --source pubchem --dry-run --max-records 100
```

Remove `--dry-run` only after reviewing the preview or dry-run output.

## Repository Map

```text
app/                  Streamlit app, Python packages, and pipeline scripts
db/init/              PostgreSQL schema loaded into fresh Docker volumes
deploy/Caddyfile      Reverse proxy configuration
docs/                 Handoff and reference documentation
tests/                Unit, adapter, and optional DB integration tests
docker-compose.yml    Local PostgreSQL + Streamlit + Caddy stack
```

## Documentation

- [Database tables and fields](docs/data-model.md)
- [Operations notes](docs/operations.md)

## Tests

```bash
pip install -r app/requirements.txt -r requirements-dev.txt
python -m pytest -q
```

DB integration tests are optional and require a configured test database:

```bash
HERG_TEST_DB=1 python -m pytest -q tests/integration
```

## Maintainer Notes

- `db/init/*.sql` runs only when PostgreSQL initializes a fresh Docker volume.
- `bioactivity_results` is the primary normalized result table.
- `source_records.raw_payload` keeps raw source provenance.
- `ic50_results` and `ic50_result_summary_v` remain for legacy hERG IC50 consumers.
