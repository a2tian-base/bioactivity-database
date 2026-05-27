CREATE OR REPLACE FUNCTION normalize_identifier(namespace TEXT, value TEXT)
RETURNS TEXT
LANGUAGE SQL
IMMUTABLE
AS $$
    SELECT CASE
        WHEN NULLIF(BTRIM(value), '') IS NULL THEN NULL
        ELSE CASE
            WHEN LOWER(BTRIM(namespace)) IN ('pubchem_cid') THEN REGEXP_REPLACE(BTRIM(value), '\s+', '', 'g')
            WHEN LOWER(BTRIM(namespace)) IN ('chembl_id', 'unii', 'a_number', 'standard_inchikey') THEN UPPER(BTRIM(value))
            ELSE LOWER(BTRIM(value))
        END
    END;
$$;

CREATE OR REPLACE FUNCTION normalize_name(value TEXT)
RETURNS TEXT
LANGUAGE SQL
IMMUTABLE
AS $$
    SELECT CASE
        WHEN NULLIF(BTRIM(value), '') IS NULL THEN NULL
        ELSE LOWER(REGEXP_REPLACE(BTRIM(value), '\s+', ' ', 'g'))
    END;
$$;

CREATE OR REPLACE FUNCTION convert_to_um(p_value NUMERIC, p_unit TEXT)
RETURNS NUMERIC
LANGUAGE SQL
IMMUTABLE
AS $$
    SELECT CASE p_unit
        WHEN 'pM' THEN p_value * 0.000001
        WHEN 'nM' THEN p_value * 0.001
        WHEN 'uM' THEN p_value
        WHEN 'mM' THEN p_value * 1000
        ELSE NULL
    END;
$$;

CREATE OR REPLACE FUNCTION invert_qualifier(p_qualifier CHAR(1))
RETURNS CHAR(1)
LANGUAGE SQL
IMMUTABLE
AS $$
    SELECT CASE p_qualifier
        WHEN '<' THEN '>'
        WHEN '>' THEN '<'
        ELSE '='
    END;
$$;

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

CREATE TABLE compounds (
    compound_id BIGSERIAL PRIMARY KEY,
    canonical_smiles TEXT,
    standard_inchi TEXT,
    standard_inchikey TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX uq_compounds_standard_inchikey
    ON compounds ((UPPER(BTRIM(standard_inchikey))))
    WHERE NULLIF(BTRIM(standard_inchikey), '') IS NOT NULL;

CREATE TABLE compound_identifiers (
    compound_identifier_id BIGSERIAL PRIMARY KEY,
    compound_id BIGINT NOT NULL REFERENCES compounds(compound_id) ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    identifier_value TEXT NOT NULL,
    normalized_value TEXT GENERATED ALWAYS AS (
        normalize_identifier(namespace, identifier_value)
    ) STORED,
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (NULLIF(BTRIM(namespace), '') IS NOT NULL),
    CHECK (NULLIF(BTRIM(identifier_value), '') IS NOT NULL),
    UNIQUE (namespace, normalized_value)
);

CREATE INDEX idx_compound_identifiers_compound_id
    ON compound_identifiers(compound_id);

CREATE UNIQUE INDEX uq_compound_identifiers_primary_per_namespace
    ON compound_identifiers(compound_id, namespace)
    WHERE is_primary;

CREATE TABLE compound_names (
    compound_name_id BIGSERIAL PRIMARY KEY,
    compound_id BIGINT NOT NULL REFERENCES compounds(compound_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    normalized_name TEXT GENERATED ALWAYS AS (
        normalize_name(name)
    ) STORED,
    name_type TEXT NOT NULL DEFAULT 'alias',
    is_preferred BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (NULLIF(BTRIM(name), '') IS NOT NULL),
    CHECK (NULLIF(BTRIM(name_type), '') IS NOT NULL),
    UNIQUE (compound_id, normalized_name)
);

CREATE INDEX idx_compound_names_compound_id
    ON compound_names(compound_id);

CREATE UNIQUE INDEX uq_compound_names_one_preferred
    ON compound_names(compound_id)
    WHERE is_preferred;

CREATE TABLE source_records (
    source_record_id BIGSERIAL PRIMARY KEY,
    source_name TEXT NOT NULL,
    source_record_key TEXT NOT NULL,
    record_type TEXT NOT NULL,
    source_release TEXT,
    source_url TEXT,
    raw_payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (NULLIF(BTRIM(source_name), '') IS NOT NULL),
    CHECK (NULLIF(BTRIM(source_record_key), '') IS NOT NULL),
    CHECK (NULLIF(BTRIM(record_type), '') IS NOT NULL),
    UNIQUE (source_name, source_record_key)
);

CREATE INDEX idx_source_records_source_name
    ON source_records(source_name);

CREATE TABLE compound_identifier_sources (
    compound_identifier_source_id BIGSERIAL PRIMARY KEY,
    compound_identifier_id BIGINT NOT NULL
        REFERENCES compound_identifiers(compound_identifier_id) ON DELETE CASCADE,
    source_record_id BIGINT NOT NULL
        REFERENCES source_records(source_record_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (compound_identifier_id, source_record_id)
);

CREATE INDEX idx_compound_identifier_sources_compound_identifier_id
    ON compound_identifier_sources(compound_identifier_id);

CREATE INDEX idx_compound_identifier_sources_source_record_id
    ON compound_identifier_sources(source_record_id);

CREATE TABLE compound_structure_assertions (
    compound_structure_assertion_id BIGSERIAL PRIMARY KEY,
    compound_id BIGINT NOT NULL
        REFERENCES compounds(compound_id) ON DELETE CASCADE,
    source_record_id BIGINT NOT NULL
        REFERENCES source_records(source_record_id) ON DELETE CASCADE,
    canonical_smiles TEXT,
    standard_inchi TEXT,
    standard_inchikey TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (compound_id, source_record_id)
);

CREATE TABLE ic50_results (
    result_id BIGSERIAL PRIMARY KEY,
    compound_id BIGINT NOT NULL REFERENCES compounds(compound_id) ON DELETE RESTRICT,
    source_record_id BIGINT NOT NULL REFERENCES source_records(source_record_id) ON DELETE RESTRICT,
    endpoint TEXT NOT NULL DEFAULT 'IC50',
    ic50_value NUMERIC(14, 6) NOT NULL CHECK (ic50_value > 0),
    ic50_unit TEXT NOT NULL CHECK (ic50_unit IN ('pM', 'nM', 'uM', 'mM')),
    qualifier CHAR(1) NOT NULL CHECK (qualifier IN ('=', '<', '>')),
    ic50_um NUMERIC(18, 6) GENERATED ALWAYS AS (
        convert_to_um(ic50_value, ic50_unit)
    ) STORED,
    pic50 NUMERIC(10, 4) GENERATED ALWAYS AS (
        ROUND((6 - LOG(10, convert_to_um(ic50_value, ic50_unit)))::NUMERIC, 4)
    ) STORED,
    pic50_qualifier CHAR(1) GENERATED ALWAYS AS (
        invert_qualifier(qualifier)
    ) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_record_id, endpoint)
);

CREATE INDEX idx_ic50_results_compound_id
    ON ic50_results(compound_id);

CREATE INDEX idx_ic50_results_source_record_id
    ON ic50_results(source_record_id);

CREATE INDEX idx_ic50_results_created_at
    ON ic50_results(created_at DESC);

CREATE TABLE endpoints (
    endpoint_id BIGSERIAL PRIMARY KEY,
    endpoint_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    spec JSONB NOT NULL,
    source_configs JSONB NOT NULL DEFAULT '{}'::JSONB,
    spec_hash TEXT NOT NULL UNIQUE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (BTRIM(endpoint_key) <> ''),
    CHECK (BTRIM(display_name) <> ''),
    CHECK (BTRIM(spec_hash) <> ''),
    CHECK (jsonb_typeof(spec) = 'object'),
    CHECK (jsonb_typeof(source_configs) = 'object')
);

CREATE INDEX idx_endpoints_active
    ON endpoints(active);

CREATE TABLE ingestion_runs (
    ingestion_run_id BIGSERIAL PRIMARY KEY,
    endpoint_id BIGINT NOT NULL REFERENCES endpoints(endpoint_id) ON DELETE RESTRICT,
    source_name TEXT NOT NULL,
    source_release TEXT,
    query_config JSONB NOT NULL DEFAULT '{}'::JSONB,
    query_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'partial')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    rows_seen INTEGER NOT NULL DEFAULT 0,
    rows_inserted INTEGER NOT NULL DEFAULT 0,
    rows_updated INTEGER NOT NULL DEFAULT 0,
    rows_skipped INTEGER NOT NULL DEFAULT 0,
    rows_failed INTEGER NOT NULL DEFAULT 0,
    qc_summary JSONB NOT NULL DEFAULT '{}'::JSONB,
    error_summary JSONB NOT NULL DEFAULT '{}'::JSONB,
    CHECK (BTRIM(source_name) <> ''),
    CHECK (BTRIM(query_hash) <> ''),
    CHECK (jsonb_typeof(query_config) = 'object'),
    CHECK (jsonb_typeof(qc_summary) = 'object'),
    CHECK (jsonb_typeof(error_summary) = 'object'),
    CHECK (rows_seen >= 0),
    CHECK (rows_inserted >= 0),
    CHECK (rows_updated >= 0),
    CHECK (rows_skipped >= 0),
    CHECK (rows_failed >= 0),
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE INDEX idx_ingestion_runs_endpoint_source_started_at
    ON ingestion_runs(endpoint_id, source_name, started_at DESC);

CREATE INDEX idx_ingestion_runs_status
    ON ingestion_runs(status);

CREATE TABLE bioactivity_results (
    result_id BIGSERIAL PRIMARY KEY,
    endpoint_id BIGINT NOT NULL REFERENCES endpoints(endpoint_id) ON DELETE RESTRICT,
    compound_id BIGINT NOT NULL REFERENCES compounds(compound_id) ON DELETE RESTRICT,
    source_record_id BIGINT NOT NULL REFERENCES source_records(source_record_id) ON DELETE RESTRICT,
    ingestion_run_id BIGINT REFERENCES ingestion_runs(ingestion_run_id) ON DELETE SET NULL,
    result_key TEXT NOT NULL,
    measurement_type TEXT NOT NULL,
    value_kind TEXT NOT NULL CHECK (
        value_kind IN ('concentration', 'percent', 'numeric', 'categorical', 'text')
    ),
    original_value NUMERIC,
    original_unit TEXT,
    original_relation TEXT,
    standard_value NUMERIC,
    standard_unit TEXT,
    standard_relation TEXT,
    p_value NUMERIC,
    p_value_relation TEXT,
    value_text TEXT,
    assay_context JSONB NOT NULL DEFAULT '{}'::JSONB,
    quality_flags JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (endpoint_id, source_record_id, result_key),
    CHECK (BTRIM(result_key) <> ''),
    CHECK (BTRIM(measurement_type) <> ''),
    CHECK (jsonb_typeof(assay_context) = 'object'),
    CHECK (jsonb_typeof(quality_flags) = 'object')
);

CREATE INDEX idx_bioactivity_results_endpoint_id
    ON bioactivity_results(endpoint_id);

CREATE INDEX idx_bioactivity_results_compound_id
    ON bioactivity_results(compound_id);

CREATE INDEX idx_bioactivity_results_source_record_id
    ON bioactivity_results(source_record_id);

CREATE INDEX idx_bioactivity_results_ingestion_run_id
    ON bioactivity_results(ingestion_run_id);

CREATE INDEX idx_bioactivity_results_measurement_type
    ON bioactivity_results(measurement_type);

WITH herg_ic50_endpoint AS (
    SELECT
        'herg_ic50'::TEXT AS endpoint_key,
        'hERG IC50'::TEXT AS display_name,
        '{
          "target": {
            "preferred_name": "hERG",
            "gene_symbol": "KCNH2",
            "organism": "Homo sapiens",
            "identifiers": {
              "chembl_target_id": "CHEMBL240",
              "ncbi_gene_id": "3757"
            }
          },
          "measurement": {
            "type": "IC50",
            "value_kind": "concentration",
            "canonical_unit": "uM",
            "supports_p_value": true,
            "p_value_name": "pIC50"
          },
          "normalization": {
            "allowed_units": ["pM", "nM", "uM", "mM"],
            "allowed_relations": ["=", "<", ">"]
          },
          "inclusion_criteria": {
            "organism": "Homo sapiens",
            "direct_target_only": true
          }
        }'::JSONB AS spec,
        '{
          "chembl": {
            "target_chembl_id": "CHEMBL240",
            "standard_type": "IC50",
            "standard_relation__in": ["=", "<", ">"],
            "data_validity_comment__isnull": true
          },
          "pubchem": {
            "target_gene_symbol": "KCNH2",
            "target_gene_id": "3757",
            "activity_name_regex": "(?i)\\bIC50\\b"
          }
        }'::JSONB AS source_configs
)
INSERT INTO endpoints (
    endpoint_key,
    display_name,
    spec,
    source_configs,
    spec_hash,
    active
)
SELECT
    endpoint_key,
    display_name,
    spec,
    source_configs,
    md5(spec::TEXT || '|' || source_configs::TEXT),
    TRUE
FROM herg_ic50_endpoint
ON CONFLICT (endpoint_key)
DO UPDATE SET
    display_name = EXCLUDED.display_name,
    spec = EXCLUDED.spec,
    source_configs = EXCLUDED.source_configs,
    spec_hash = EXCLUDED.spec_hash,
    active = EXCLUDED.active;

CREATE TRIGGER trg_set_compounds_updated_at
BEFORE UPDATE
ON compounds
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_set_compound_identifiers_updated_at
BEFORE UPDATE
ON compound_identifiers
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_set_compound_names_updated_at
BEFORE UPDATE
ON compound_names
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_set_source_records_updated_at
BEFORE UPDATE
ON source_records
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_set_ic50_results_updated_at
BEFORE UPDATE
ON ic50_results
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_set_endpoints_updated_at
BEFORE UPDATE
ON endpoints
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_set_bioactivity_results_updated_at
BEFORE UPDATE
ON bioactivity_results
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE OR REPLACE FUNCTION resolve_compound_id(p_id_type TEXT, p_id_value TEXT)
RETURNS BIGINT
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_id_type TEXT := LOWER(BTRIM(p_id_type));
    v_id_value TEXT := NULLIF(BTRIM(p_id_value), '');
    v_compound_id BIGINT;
BEGIN
    IF v_id_value IS NULL OR v_id_type IS NULL OR v_id_type = '' THEN
        RETURN NULL;
    END IF;

    IF v_id_type IN ('standard_inchikey', 'inchikey') THEN
        SELECT compound_id
        INTO v_compound_id
        FROM compounds
        WHERE UPPER(BTRIM(standard_inchikey)) = UPPER(v_id_value)
        LIMIT 1;

        RETURN v_compound_id;
    END IF;

    SELECT compound_id
    INTO v_compound_id
    FROM compound_identifiers
    WHERE LOWER(BTRIM(namespace)) = v_id_type
      AND normalized_value = normalize_identifier(v_id_type, v_id_value)
    LIMIT 1;

    RETURN v_compound_id;
END;
$$;

CREATE OR REPLACE FUNCTION resolve_compound_by_keys(
    p_standard_inchikey TEXT DEFAULT NULL,
    p_identifiers JSONB DEFAULT '[]'::JSONB
) RETURNS BIGINT
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_identifiers JSONB := COALESCE(p_identifiers, '[]'::JSONB);
    v_standard_inchikey TEXT := NULLIF(BTRIM(p_standard_inchikey), '');
    v_match_count INTEGER;
    v_inchikey_match_id BIGINT;
    v_identifier_match_id BIGINT;
    v_identifier_match_count INTEGER;
BEGIN
    IF v_standard_inchikey IS NOT NULL THEN
        SELECT COUNT(*), MIN(compound_id)
        INTO v_match_count, v_inchikey_match_id
        FROM compounds
        WHERE UPPER(BTRIM(standard_inchikey)) = UPPER(v_standard_inchikey);

        IF v_match_count > 1 THEN
            RAISE EXCEPTION 'standard_inchikey matches multiple compounds.';
        END IF;
    END IF;

    WITH input_identifiers AS (
        SELECT
            LOWER(BTRIM(namespace)) AS namespace,
            NULLIF(BTRIM(value), '') AS identifier_value,
            normalize_identifier(LOWER(BTRIM(namespace)), NULLIF(BTRIM(value), '')) AS normalized_value
        FROM jsonb_to_recordset(v_identifiers) AS x(namespace TEXT, value TEXT, is_primary BOOLEAN)
    ),
    filtered_identifiers AS (
        SELECT *
        FROM input_identifiers
        WHERE namespace IS NOT NULL
          AND namespace <> ''
          AND identifier_value IS NOT NULL
          AND normalized_value IS NOT NULL
          AND normalized_value <> ''
    )
    SELECT COUNT(DISTINCT ci.compound_id), MIN(ci.compound_id)
    INTO v_identifier_match_count, v_identifier_match_id
    FROM compound_identifiers ci
    JOIN filtered_identifiers fi
      ON LOWER(BTRIM(ci.namespace)) = fi.namespace
     AND ci.normalized_value = fi.normalized_value;

    IF v_inchikey_match_id IS NOT NULL THEN
        IF v_identifier_match_count > 1 THEN
            RAISE EXCEPTION 'Provided identifiers match multiple compounds.';
        ELSIF v_identifier_match_count = 1 AND v_identifier_match_id <> v_inchikey_match_id THEN
            RAISE EXCEPTION 'Provided identifiers conflict with standard_inchikey match.';
        END IF;
        RETURN v_inchikey_match_id;
    END IF;

    IF v_identifier_match_count > 1 THEN
        RAISE EXCEPTION 'Provided identifiers match multiple compounds.';
    ELSIF v_identifier_match_count = 1 THEN
        RETURN v_identifier_match_id;
    END IF;

    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION register_compound_v2(
    p_identifiers JSONB DEFAULT '[]'::JSONB,
    p_names JSONB DEFAULT '[]'::JSONB,
    p_canonical_smiles TEXT DEFAULT NULL,
    p_standard_inchi TEXT DEFAULT NULL,
    p_standard_inchikey TEXT DEFAULT NULL
) RETURNS BIGINT
LANGUAGE plpgsql
AS $$
DECLARE
    v_identifiers JSONB := COALESCE(p_identifiers, '[]'::JSONB);
    v_names JSONB := COALESCE(p_names, '[]'::JSONB);
    v_canonical_smiles TEXT := NULLIF(BTRIM(p_canonical_smiles), '');
    v_standard_inchi TEXT := NULLIF(BTRIM(p_standard_inchi), '');
    v_standard_inchikey TEXT := NULLIF(BTRIM(p_standard_inchikey), '');
    v_compound_id BIGINT;
BEGIN
    v_compound_id := resolve_compound_by_keys(v_standard_inchikey, v_identifiers);

    IF v_compound_id IS NULL THEN
        INSERT INTO compounds (
            canonical_smiles,
            standard_inchi,
            standard_inchikey
        )
        VALUES (
            v_canonical_smiles,
            v_standard_inchi,
            v_standard_inchikey
        )
        RETURNING compound_id INTO v_compound_id;
    ELSE
        UPDATE compounds c
        SET
            canonical_smiles = COALESCE(NULLIF(BTRIM(c.canonical_smiles), ''), v_canonical_smiles),
            standard_inchi = COALESCE(NULLIF(BTRIM(c.standard_inchi), ''), v_standard_inchi),
            standard_inchikey = COALESCE(NULLIF(BTRIM(c.standard_inchikey), ''), v_standard_inchikey)
        WHERE c.compound_id = v_compound_id;
    END IF;

    WITH input_identifiers AS (
        SELECT
            LOWER(BTRIM(namespace)) AS namespace,
            NULLIF(BTRIM(value), '') AS identifier_value,
            COALESCE(is_primary, false) AS is_primary,
            normalize_identifier(LOWER(BTRIM(namespace)), NULLIF(BTRIM(value), '')) AS normalized_value
        FROM jsonb_to_recordset(v_identifiers) AS x(namespace TEXT, value TEXT, is_primary BOOLEAN)
    ),
    filtered_identifiers AS (
        SELECT DISTINCT ON (namespace, normalized_value)
            namespace,
            identifier_value,
            normalized_value,
            is_primary
        FROM input_identifiers
        WHERE namespace IS NOT NULL
          AND namespace <> ''
          AND identifier_value IS NOT NULL
          AND normalized_value IS NOT NULL
          AND normalized_value <> ''
        ORDER BY namespace, normalized_value, is_primary DESC
    ),
    identifiers_to_insert AS (
        SELECT
            fi.namespace,
            fi.identifier_value,
            CASE
                WHEN fi.is_primary AND NOT EXISTS (
                    SELECT 1
                    FROM compound_identifiers ci
                    WHERE ci.compound_id = v_compound_id
                      AND LOWER(BTRIM(ci.namespace)) = fi.namespace
                      AND ci.is_primary
                )
                THEN TRUE
                ELSE FALSE
            END AS is_primary
        FROM filtered_identifiers fi
    )
    INSERT INTO compound_identifiers (
        compound_id,
        namespace,
        identifier_value,
        is_primary
    )
    SELECT
        v_compound_id,
        namespace,
        identifier_value,
        is_primary
    FROM identifiers_to_insert
    ON CONFLICT (namespace, normalized_value) DO NOTHING;

    WITH input_names AS (
        SELECT
            NULLIF(BTRIM(name), '') AS name,
            COALESCE(NULLIF(BTRIM(name_type), ''), 'alias') AS name_type,
            COALESCE(is_preferred, false) AS is_preferred,
            normalize_name(NULLIF(BTRIM(name), '')) AS normalized_name
        FROM jsonb_to_recordset(v_names) AS x(name TEXT, name_type TEXT, is_preferred BOOLEAN)
    ),
    filtered_names AS (
        SELECT DISTINCT ON (normalized_name)
            name,
            name_type,
            normalized_name,
            is_preferred
        FROM input_names
        WHERE name IS NOT NULL
          AND normalized_name IS NOT NULL
          AND normalized_name <> ''
        ORDER BY normalized_name, is_preferred DESC
    ),
    names_to_insert AS (
        SELECT
            fn.name,
            fn.name_type,
            CASE
                WHEN fn.is_preferred AND NOT EXISTS (
                    SELECT 1
                    FROM compound_names cn
                    WHERE cn.compound_id = v_compound_id
                      AND cn.is_preferred
                )
                THEN TRUE
                ELSE FALSE
            END AS is_preferred
        FROM filtered_names fn
    )
    INSERT INTO compound_names (
        compound_id,
        name,
        name_type,
        is_preferred
    )
    SELECT
        v_compound_id,
        name,
        name_type,
        is_preferred
    FROM names_to_insert
    ON CONFLICT (compound_id, normalized_name) DO NOTHING;

    RETURN v_compound_id;
END;
$$;

CREATE OR REPLACE FUNCTION upsert_source_record(
    p_source_name TEXT,
    p_source_record_key TEXT,
    p_record_type TEXT,
    p_source_release TEXT DEFAULT NULL,
    p_source_url TEXT DEFAULT NULL,
    p_raw_payload JSONB DEFAULT '{}'::JSONB
) RETURNS BIGINT
LANGUAGE plpgsql
AS $$
DECLARE
    v_source_name TEXT := NULLIF(BTRIM(p_source_name), '');
    v_source_record_key TEXT := NULLIF(BTRIM(p_source_record_key), '');
    v_record_type TEXT := NULLIF(BTRIM(p_record_type), '');
    v_source_release TEXT := NULLIF(BTRIM(p_source_release), '');
    v_source_url TEXT := NULLIF(BTRIM(p_source_url), '');
    v_raw_payload JSONB := COALESCE(p_raw_payload, '{}'::JSONB);
    v_source_record_id BIGINT;
BEGIN
    IF v_source_name IS NULL OR v_source_record_key IS NULL OR v_record_type IS NULL THEN
        RAISE EXCEPTION 'source_name, source_record_key, and record_type are required.';
    END IF;

    INSERT INTO source_records (
        source_name,
        source_record_key,
        record_type,
        source_release,
        source_url,
        raw_payload
    )
    VALUES (
        v_source_name,
        v_source_record_key,
        v_record_type,
        v_source_release,
        v_source_url,
        v_raw_payload
    )
    ON CONFLICT (source_name, source_record_key)
    DO UPDATE SET
        record_type = EXCLUDED.record_type,
        source_release = COALESCE(NULLIF(EXCLUDED.source_release, ''), source_records.source_release),
        source_url = COALESCE(NULLIF(EXCLUDED.source_url, ''), source_records.source_url),
        raw_payload = CASE
            WHEN EXCLUDED.raw_payload IS NULL OR EXCLUDED.raw_payload = '{}'::JSONB
                THEN source_records.raw_payload
            ELSE EXCLUDED.raw_payload
        END
    RETURNING source_record_id INTO v_source_record_id;

    RETURN v_source_record_id;
END;
$$;

CREATE OR REPLACE FUNCTION upsert_ic50_result(
    p_compound_id BIGINT,
    p_source_record_id BIGINT,
    p_endpoint TEXT,
    p_ic50_value NUMERIC,
    p_ic50_unit TEXT,
    p_qualifier CHAR(1)
) RETURNS TABLE (
    result_id BIGINT,
    ic50_um NUMERIC,
    pic50 NUMERIC,
    pic50_qualifier CHAR(1)
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    INSERT INTO ic50_results (
        compound_id,
        source_record_id,
        endpoint,
        ic50_value,
        ic50_unit,
        qualifier
    )
    VALUES (
        p_compound_id,
        p_source_record_id,
        COALESCE(NULLIF(BTRIM(p_endpoint), ''), 'IC50'),
        p_ic50_value,
        p_ic50_unit,
        p_qualifier
    )
    ON CONFLICT (source_record_id, endpoint)
    DO UPDATE SET
        compound_id = EXCLUDED.compound_id,
        ic50_value = EXCLUDED.ic50_value,
        ic50_unit = EXCLUDED.ic50_unit,
        qualifier = EXCLUDED.qualifier
    RETURNING ic50_results.result_id, ic50_results.ic50_um, ic50_results.pic50, ic50_results.pic50_qualifier;
END;
$$;

CREATE OR REPLACE VIEW compound_summary_v AS
SELECT
    c.compound_id,
    identifiers.a_number,
    identifiers.unii,
    identifiers.pubchem_cid,
    identifiers.chembl_id,
    names.preferred_name,
    names.common_names,
    c.canonical_smiles,
    c.standard_inchi,
    c.standard_inchikey,
    c.created_at,
    c.updated_at
FROM compounds c
LEFT JOIN LATERAL (
    SELECT
        (ARRAY_AGG(identifier_value ORDER BY is_primary DESC, compound_identifier_id ASC)
            FILTER (WHERE namespace = 'a_number'))[1] AS a_number,
        (ARRAY_AGG(identifier_value ORDER BY is_primary DESC, compound_identifier_id ASC)
            FILTER (WHERE namespace = 'unii'))[1] AS unii,
        (ARRAY_AGG(identifier_value ORDER BY is_primary DESC, compound_identifier_id ASC)
            FILTER (WHERE namespace = 'chembl_id'))[1] AS chembl_id,
        (ARRAY_AGG(identifier_value ORDER BY is_primary DESC, compound_identifier_id ASC)
            FILTER (WHERE namespace = 'pubchem_cid' AND identifier_value ~ '^\d+$'))[1]::BIGINT AS pubchem_cid
    FROM compound_identifiers
    WHERE compound_id = c.compound_id
) identifiers ON TRUE
LEFT JOIN LATERAL (
    SELECT
        (ARRAY_AGG(name ORDER BY is_preferred DESC, compound_name_id ASC)
            FILTER (WHERE is_preferred OR name_type = 'preferred'))[1] AS preferred_name,
        COALESCE(
            ARRAY_REMOVE(
                ARRAY_AGG(name ORDER BY name) FILTER (WHERE NOT is_preferred AND name_type <> 'preferred'),
                NULL
            ),
            ARRAY[]::TEXT[]
        ) AS common_names
    FROM compound_names
    WHERE compound_id = c.compound_id
) names ON TRUE;

CREATE OR REPLACE VIEW ic50_result_summary_v AS
SELECT
    r.result_id,
    r.compound_id,
    r.source_record_id,
    r.endpoint,
    r.ic50_value,
    r.ic50_unit,
    r.qualifier,
    r.ic50_um,
    r.pic50,
    r.pic50_qualifier,
    r.created_at,
    r.updated_at,
    s.source_name,
    s.source_record_key,
    s.source_release,
    s.source_url,
    c.preferred_name,
    c.a_number,
    c.unii,
    c.pubchem_cid,
    c.chembl_id,
    c.standard_inchikey,
    (
        CASE
            WHEN NULLIF(BTRIM(c.preferred_name), '') IS NOT NULL THEN c.preferred_name
            WHEN NULLIF(BTRIM(c.chembl_id), '') IS NOT NULL THEN 'ChEMBL:' || c.chembl_id
            WHEN NULLIF(BTRIM(c.a_number), '') IS NOT NULL THEN 'A-number:' || c.a_number
            WHEN NULLIF(BTRIM(c.unii), '') IS NOT NULL THEN 'UNII:' || c.unii
            WHEN c.pubchem_cid IS NOT NULL THEN 'PubChem:' || c.pubchem_cid::TEXT
            ELSE 'compound_id:' || r.compound_id::TEXT
        END
        || CASE
            WHEN NULLIF(BTRIM(c.standard_inchikey), '') IS NOT NULL
                THEN ' | InChIKey:' || c.standard_inchikey
            ELSE ''
        END
    ) AS compound_label
FROM ic50_results r
JOIN source_records s ON s.source_record_id = r.source_record_id
JOIN compound_summary_v c ON c.compound_id = r.compound_id;
