# Operations

Use this file for commands that are useful during maintenance but too detailed for the README.

## CSV Uploads

The Streamlit `Upload CSV` tab can import compounds and IC50 results. It also provides downloadable templates.

Compound CSV columns:

```csv
a_number,unii,pubchem_cid,chembl_id,standard_inchikey,standard_inchi,canonical_smiles,preferred_name,common_names
```

IC50 CSV columns:

```csv
id_type,id_value,ic50_value,ic50_unit,qualifier,source_name,source_record_key,source_release,source_url
```

Notes:
- `common_names` can be pipe-separated or comma-separated.
- `id_type` should match an identifier namespace such as `chembl_id`, `pubchem_cid`, `unii`, `a_number`, or `standard_inchikey`.
- `ic50_unit` must be `pM`, `nM`, `uM`, or `mM`.
- `qualifier` must be `=`, `<`, or `>`.

## Source Ingestion

Run scripts inside the `frontend` container so they use the same dependencies and network as the app.

Generic endpoint ingestion is the preferred path. Endpoint-specific settings come from the `endpoints.source_configs` JSON, and CLI source flags act as explicit overrides.

ChEMBL endpoint ingestion:

```powershell
docker compose exec frontend python /app/scripts/ingest_endpoint.py --endpoint-key herg_ic50 --source chembl --dry-run --max-records 100
docker compose exec frontend python /app/scripts/ingest_endpoint.py --endpoint-key cyp3a4_ic50 --source chembl --dry-run --max-records 100
docker compose exec frontend python /app/scripts/ingest_endpoint.py --endpoint-key herg_ic50 --source chembl
```

PubChem endpoint ingestion:

```powershell
docker compose exec frontend python /app/scripts/ingest_endpoint.py --endpoint-key herg_ic50 --source pubchem --dry-run --max-records 100
docker compose exec frontend python /app/scripts/ingest_endpoint.py --endpoint-key cyp3a4_ic50 --source pubchem --dry-run --max-records 100
docker compose exec frontend python /app/scripts/ingest_endpoint.py --endpoint-key herg_ic50 --source pubchem
```

The old hERG script names remain callable as compatibility wrappers around generic endpoint ingestion with `--endpoint-key herg_ic50` as the default.

ChEMBL hERG compatibility wrapper:

```powershell
docker compose exec frontend python /app/scripts/ingest_chembl_herg.py --dry-run --max-records 100
docker compose exec frontend python /app/scripts/ingest_chembl_herg.py
```

Useful ChEMBL flags:

```text
--target-chembl-id CHEMBL240
--standard-type IC50
--relations =,<,>
--activity-page-size 1000
--molecule-batch-size 150
--max-records 1000
--errors-path /tmp/chembl-errors.jsonl
--stats-path /tmp/chembl-stats.json
```

PubChem hERG compatibility wrapper:

```powershell
docker compose exec frontend python /app/scripts/ingest_pubchem_herg.py --dry-run --max-records 100
docker compose exec frontend python /app/scripts/ingest_pubchem_herg.py
```

Useful PubChem flags:

```text
--target-gene-symbol KCNH2
--target-gene-id 3757
--activity-name-regex "(?i)\bic50\b"
--cid-batch-size 150
--max-records 1000
--errors-path /tmp/pubchem-errors.jsonl
--stats-path /tmp/pubchem-stats.json
```

### IC50 compatibility strategy

`bioactivity_results` is the primary endpoint result table. The legacy `ic50_results` table and `ic50_result_summary_v` view are retained for existing hERG IC50 consumers. IC50 ingestion paths intentionally dual-write to both `ic50_results` and `bioactivity_results`; tests assert that the duplicated values stay consistent.

## Identifier Enrichment

Attach curated identifiers from CSV:

```powershell
docker compose cp .\identifier_enrichment.csv frontend:/tmp/identifier_enrichment.csv
docker compose exec frontend python /app/scripts/enrich_compound_identifiers.py /tmp/identifier_enrichment.csv --dry-run
docker compose exec frontend python /app/scripts/enrich_compound_identifiers.py /tmp/identifier_enrichment.csv
```

Curated identifier CSV columns:

```csv
match_inchikey,match_chembl_id,match_pubchem_cid,match_unii,add_namespace,add_value,is_primary,source_record_key
```

Build UniChem candidate CSVs for review:

```powershell
docker compose exec frontend python /app/scripts/build_unichem_identifier_candidates.py /tmp/unichem_candidates.csv --target-namespace unii --limit 100
```

Attach identifiers directly from UniChem:

```powershell
docker compose exec frontend python /app/scripts/enrich_identifiers_from_unichem.py --dry-run --max-records 100
docker compose exec frontend python /app/scripts/enrich_identifiers_from_unichem.py
```

Supported UniChem target namespaces are `chembl_id`, `pubchem_cid`, and `unii`.

## Structure Enrichment

Backfill missing structure fields from ChEMBL and PubChem:

```powershell
docker compose exec frontend python /app/scripts/enrich_structures.py --dry-run --max-records 100
docker compose exec frontend python /app/scripts/enrich_structures.py
```

Provider-specific wrappers are also available:

```powershell
docker compose exec frontend python /app/scripts/enrich_structures_from_chembl.py --dry-run
docker compose exec frontend python /app/scripts/enrich_structures_from_pubchem.py --dry-run
```

Useful flags:

```text
--provider all|chembl|pubchem
--batch-size 150
--max-records 1000
--errors-path /tmp/structure-errors.jsonl
--unmatched-path /tmp/structure-unmatched.jsonl
--conflicts-path /tmp/structure-conflicts.jsonl
--stats-path /tmp/structure-stats.json
```

## Validation Queries

```sql
SELECT COUNT(*) AS compounds_n FROM compound_summary_v;
SELECT COUNT(*) AS endpoint_results_n FROM bioactivity_results;
SELECT COUNT(*) AS legacy_ic50_results_n FROM ic50_result_summary_v;

SELECT
  e.endpoint_key,
  b.result_id,
  b.compound_id,
  b.measurement_type,
  b.value_kind,
  b.standard_value,
  b.standard_unit,
  b.p_value,
  s.source_name,
  s.source_record_key
FROM bioactivity_results b
JOIN endpoints e ON e.endpoint_id = b.endpoint_id
JOIN source_records s ON s.source_record_id = b.source_record_id
ORDER BY b.result_id DESC
LIMIT 20;

SELECT
  result_id,
  compound_label,
  ic50_value,
  ic50_unit,
  qualifier,
  ic50_um,
  pic50,
  pic50_qualifier,
  source_name,
  source_record_key
FROM ic50_result_summary_v
ORDER BY result_id DESC
LIMIT 20;
```

## Deployment Notes

For a single VM deployment:

1. Install Docker.
2. Copy the repository to the server.
3. Create `.env` from `.env.example`.
4. Set `POSTGRES_PASSWORD`, `APP_DOMAIN`, `HTTP_PORT`, and `HTTPS_PORT`.
5. Run `docker compose up -d --build`.

For managed PostgreSQL plus an app container:

1. Run `db/init/001_schema.sql` against the managed database.
2. Build/deploy `app/` as the Streamlit container.
3. Set `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, and `DB_PASSWORD` in the app runtime.

## Troubleshooting

- `db/init/001_schema.sql` did not apply: reset the Docker volume with `docker compose down -v`, then restart.
- Integration tests are skipped: set `HERG_TEST_DB=1`.
- Source scripts fail on schema checks: the database volume was likely initialized with an older schema.
- Domain/TLS issues: verify DNS, inbound ports 80/443, and Caddy logs with `docker compose logs -f caddy`.
