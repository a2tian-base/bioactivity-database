CREATE TABLE IF NOT EXISTS compounds (
    compound_id BIGSERIAL PRIMARY KEY,
    a_number TEXT,
    unii TEXT,
    pubchem_cid BIGINT CHECK (pubchem_cid > 0),
    chembl_id TEXT,
    smiles TEXT,
    common_names TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        COALESCE(
            NULLIF(BTRIM(a_number), ''),
            NULLIF(BTRIM(unii), ''),
            CASE WHEN pubchem_cid IS NULL THEN NULL ELSE pubchem_cid::TEXT END,
            NULLIF(BTRIM(chembl_id), '')
        ) IS NOT NULL
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_compounds_a_number
    ON compounds ((LOWER(BTRIM(a_number))))
    WHERE NULLIF(BTRIM(a_number), '') IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_compounds_unii
    ON compounds ((LOWER(BTRIM(unii))))
    WHERE NULLIF(BTRIM(unii), '') IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_compounds_pubchem_cid
    ON compounds (pubchem_cid)
    WHERE pubchem_cid IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_compounds_chembl_id
    ON compounds ((LOWER(BTRIM(chembl_id))))
    WHERE NULLIF(BTRIM(chembl_id), '') IS NOT NULL;

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_set_compounds_updated_at ON compounds;

CREATE TRIGGER trg_set_compounds_updated_at
BEFORE UPDATE
ON compounds
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE OR REPLACE FUNCTION register_compound(
    p_a_number TEXT DEFAULT NULL,
    p_unii TEXT DEFAULT NULL,
    p_pubchem_cid BIGINT DEFAULT NULL,
    p_chembl_id TEXT DEFAULT NULL,
    p_smiles TEXT DEFAULT NULL,
    p_common_names TEXT[] DEFAULT NULL
)
RETURNS BIGINT
LANGUAGE plpgsql
AS $$
DECLARE
    v_a_number TEXT := NULLIF(BTRIM(p_a_number), '');
    v_unii TEXT := NULLIF(BTRIM(p_unii), '');
    v_pubchem_cid BIGINT := p_pubchem_cid;
    v_chembl_id TEXT := NULLIF(BTRIM(p_chembl_id), '');
    v_smiles TEXT := NULLIF(BTRIM(p_smiles), '');
    v_common_names TEXT[];
    v_match_count INTEGER;
    v_compound_id BIGINT;
BEGIN
    IF v_unii IS NOT NULL THEN
        v_unii := UPPER(v_unii);
    END IF;

    IF v_chembl_id IS NOT NULL THEN
        v_chembl_id := UPPER(v_chembl_id);
    END IF;

    SELECT
        COALESCE(
            ARRAY(
                SELECT DISTINCT cleaned_name
                FROM (
                    SELECT NULLIF(BTRIM(name), '') AS cleaned_name
                    FROM UNNEST(COALESCE(p_common_names, ARRAY[]::TEXT[])) AS src(name)
                ) cleaned
                WHERE cleaned_name IS NOT NULL
                ORDER BY cleaned_name
            ),
            ARRAY[]::TEXT[]
        )
    INTO v_common_names;

    IF v_a_number IS NULL
        AND v_unii IS NULL
        AND v_pubchem_cid IS NULL
        AND v_chembl_id IS NULL THEN
        RAISE EXCEPTION 'At least one identifier must be provided (a_number, unii, pubchem_cid, chembl_id).';
    END IF;

    SELECT COUNT(*), MIN(compound_id)
    INTO v_match_count, v_compound_id
    FROM compounds
    WHERE (v_a_number IS NOT NULL AND LOWER(BTRIM(a_number)) = LOWER(v_a_number))
       OR (v_unii IS NOT NULL AND LOWER(BTRIM(unii)) = LOWER(v_unii))
       OR (v_pubchem_cid IS NOT NULL AND pubchem_cid = v_pubchem_cid)
       OR (v_chembl_id IS NOT NULL AND LOWER(BTRIM(chembl_id)) = LOWER(v_chembl_id));

    IF v_match_count > 1 THEN
        RAISE EXCEPTION 'Provided identifiers match multiple compounds. Resolve identifier conflicts before loading.';
    ELSIF v_match_count = 0 THEN
        INSERT INTO compounds (
            a_number,
            unii,
            pubchem_cid,
            chembl_id,
            smiles,
            common_names
        )
        VALUES (
            v_a_number,
            v_unii,
            v_pubchem_cid,
            v_chembl_id,
            v_smiles,
            v_common_names
        )
        RETURNING compound_id INTO v_compound_id;
    ELSE
        UPDATE compounds c
        SET
            a_number = COALESCE(c.a_number, v_a_number),
            unii = COALESCE(c.unii, v_unii),
            pubchem_cid = COALESCE(c.pubchem_cid, v_pubchem_cid),
            chembl_id = COALESCE(c.chembl_id, v_chembl_id),
            smiles = COALESCE(c.smiles, v_smiles),
            common_names = CASE
                WHEN CARDINALITY(v_common_names) = 0 THEN c.common_names
                ELSE ARRAY(
                    SELECT DISTINCT merged_name
                    FROM (
                        SELECT NULLIF(BTRIM(name), '') AS merged_name
                        FROM UNNEST(c.common_names || v_common_names) AS src(name)
                    ) merged
                    WHERE merged_name IS NOT NULL
                    ORDER BY merged_name
                )
            END
        WHERE c.compound_id = v_compound_id
        RETURNING compound_id INTO v_compound_id;
    END IF;

    RETURN v_compound_id;
END;
$$;

CREATE TABLE IF NOT EXISTS ic50_results (
    result_id BIGSERIAL PRIMARY KEY,
    compound_id BIGINT NOT NULL REFERENCES compounds(compound_id) ON DELETE RESTRICT,
    ic50_value NUMERIC(14, 6) NOT NULL CHECK (ic50_value > 0),
    ic50_unit TEXT NOT NULL CHECK (ic50_unit IN ('pM', 'nM', 'uM', 'mM')),
    qualifier CHAR(1) NOT NULL CHECK (qualifier IN ('=', '<', '>')),
    ic50_nm NUMERIC(18, 6) NOT NULL CHECK (ic50_nm > 0),
    pic50 NUMERIC(10, 4) NOT NULL,
    source_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ic50_results_compound_id ON ic50_results(compound_id);
CREATE INDEX IF NOT EXISTS idx_ic50_results_created_at ON ic50_results(created_at DESC);

CREATE OR REPLACE FUNCTION convert_to_nm(p_value NUMERIC, p_unit TEXT)
RETURNS NUMERIC
LANGUAGE SQL
IMMUTABLE
AS $$
    SELECT CASE p_unit
        WHEN 'pM' THEN p_value * 0.001
        WHEN 'nM' THEN p_value
        WHEN 'uM' THEN p_value * 1000
        WHEN 'mM' THEN p_value * 1000000
        ELSE NULL
    END;
$$;

CREATE OR REPLACE FUNCTION set_derived_ic50_fields()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.ic50_nm := convert_to_nm(NEW.ic50_value, NEW.ic50_unit);

    IF NEW.ic50_nm IS NULL OR NEW.ic50_nm <= 0 THEN
        RAISE EXCEPTION 'Unable to derive ic50_nm from unit % and value %', NEW.ic50_unit, NEW.ic50_value;
    END IF;

    NEW.pic50 := ROUND((9 - LOG(10, NEW.ic50_nm))::NUMERIC, 4);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_set_derived_ic50_fields ON ic50_results;

CREATE TRIGGER trg_set_derived_ic50_fields
BEFORE INSERT OR UPDATE OF ic50_value, ic50_unit
ON ic50_results
FOR EACH ROW
EXECUTE FUNCTION set_derived_ic50_fields();
