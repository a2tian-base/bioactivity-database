import os
import math
from typing import Dict, List, Optional

import pandas as pd
import psycopg
from psycopg.errors import UniqueViolation
import streamlit as st


st.set_page_config(page_title="hERG IC50 Database", layout="wide")

ALLOWED_IDENTIFIER_TYPES = {"a_number", "unii", "pubchem_cid", "chembl_id"}
ALLOWED_IC50_UNITS = {"pM", "nM", "uM", "mM"}
ALLOWED_QUALIFIERS = {"=", "<", ">"}


def get_conn() -> psycopg.Connection:
    return psycopg.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "herg"),
        user=os.getenv("DB_USER", "herg_user"),
        password=os.getenv("DB_PASSWORD", "change_me"),
    )


def clean_text(value: object) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def parse_optional_positive_int(value: object) -> Optional[int]:
    text = clean_text(value)
    if not text:
        return None
    try:
        number = float(text)
    except ValueError as exc:
        raise ValueError(f"Expected positive integer, got '{text}'.") from exc
    if number <= 0 or not number.is_integer():
        raise ValueError(f"Expected positive integer, got '{text}'.")
    return int(number)


def parse_common_names(common_names_input: str) -> List[str]:
    names = [name.strip() for name in common_names_input.split(",")]
    return [name for name in names if name]


def parse_common_names_field(value: object) -> List[str]:
    text = clean_text(value)
    if not text:
        return []
    delimiter = "|" if "|" in text else ","
    names = [name.strip() for name in text.split(delimiter)]
    return [name for name in names if name]


def normalize_ic50_unit(unit_value: object) -> str:
    raw = clean_text(unit_value).replace("µ", "u").replace("μ", "u")
    mapping = {
        "pm": "pM",
        "nm": "nM",
        "um": "uM",
        "mm": "mM",
    }
    unit = mapping.get(raw.lower(), raw)
    if unit not in ALLOWED_IC50_UNITS:
        raise ValueError(f"Invalid ic50_unit '{raw}'. Allowed: pM, nM, uM, mM.")
    return unit


def normalize_qualifier(qualifier_value: object) -> str:
    qualifier = clean_text(qualifier_value)
    if qualifier not in ALLOWED_QUALIFIERS:
        raise ValueError("Invalid qualifier. Allowed values are '=', '<', '>'.")
    return qualifier


def build_compound_label(compound: Dict) -> str:
    id_parts = []
    if compound["a_number"]:
        id_parts.append(f"A-number:{compound['a_number']}")
    if compound["unii"]:
        id_parts.append(f"UNII:{compound['unii']}")
    if compound["pubchem_cid"]:
        id_parts.append(f"PubChem:{compound['pubchem_cid']}")
    if compound["chembl_id"]:
        id_parts.append(f"ChEMBL:{compound['chembl_id']}")

    alias_text = ", ".join(compound["common_names"]) if compound["common_names"] else ""
    id_text = " | ".join(id_parts) if id_parts else f"compound_id:{compound['compound_id']}"
    if alias_text:
        id_text = f"{id_text} | aliases:{alias_text}"
    return f"{id_text} (id={compound['compound_id']})"


def fetch_compounds() -> List[Dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                compound_id,
                a_number,
                unii,
                pubchem_cid,
                chembl_id,
                smiles,
                common_names,
                created_at
            FROM compounds
            ORDER BY
                COALESCE(a_number, chembl_id, unii, pubchem_cid::TEXT, compound_id::TEXT) ASC
            """
        )
        rows = cur.fetchall()
    return [
        {
            "compound_id": row[0],
            "a_number": row[1],
            "unii": row[2],
            "pubchem_cid": row[3],
            "chembl_id": row[4],
            "smiles": row[5],
            "common_names": row[6] or [],
            "created_at": row[7],
        }
        for row in rows
    ]


def register_compound_with_cursor(
    cur: psycopg.Cursor,
    a_number: str,
    unii: str,
    pubchem_cid: Optional[int],
    chembl_id: str,
    smiles: str,
    common_names: List[str],
) -> int:
    cur.execute(
        """
        SELECT register_compound(%s, %s, %s, %s, %s, %s::text[])
        """,
        (
            a_number.strip(),
            unii.strip(),
            pubchem_cid,
            chembl_id.strip(),
            smiles.strip(),
            common_names,
        ),
    )
    row = cur.fetchone()
    return row[0]


def register_compound(
    a_number: str,
    unii: str,
    pubchem_cid: Optional[int],
    chembl_id: str,
    smiles: str,
    common_names: List[str],
) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        return register_compound_with_cursor(
            cur=cur,
            a_number=a_number,
            unii=unii,
            pubchem_cid=pubchem_cid,
            chembl_id=chembl_id,
            smiles=smiles,
            common_names=common_names,
        )


def insert_ic50_result_with_cursor(
    cur: psycopg.Cursor,
    compound_id: int,
    ic50_value: float,
    ic50_unit: str,
    qualifier: str,
    source_ref: str,
) -> Dict:
    cur.execute(
        """
        INSERT INTO ic50_results (compound_id, ic50_value, ic50_unit, qualifier, source_ref)
        VALUES (%s, %s, %s, %s, NULLIF(%s, ''))
        RETURNING result_id, ic50_nm, pic50
        """,
        (compound_id, ic50_value, ic50_unit, qualifier, source_ref),
    )
    row = cur.fetchone()
    return {"result_id": row[0], "ic50_nm": row[1], "pic50": row[2]}


def insert_ic50_result(
    compound_id: int, ic50_value: float, ic50_unit: str, qualifier: str, source_ref: str
) -> Dict:
    with get_conn() as conn, conn.cursor() as cur:
        return insert_ic50_result_with_cursor(
            cur=cur,
            compound_id=compound_id,
            ic50_value=ic50_value,
            ic50_unit=ic50_unit,
            qualifier=qualifier,
            source_ref=source_ref,
        )


def resolve_compound_id_by_identifier(cur: psycopg.Cursor, id_type: str, id_value: str) -> Optional[int]:
    if id_type == "a_number":
        cur.execute(
            """
            SELECT compound_id
            FROM compounds
            WHERE LOWER(BTRIM(a_number)) = LOWER(BTRIM(%s))
            """,
            (id_value,),
        )
    elif id_type == "unii":
        cur.execute(
            """
            SELECT compound_id
            FROM compounds
            WHERE LOWER(BTRIM(unii)) = LOWER(BTRIM(%s))
            """,
            (id_value,),
        )
    elif id_type == "chembl_id":
        cur.execute(
            """
            SELECT compound_id
            FROM compounds
            WHERE LOWER(BTRIM(chembl_id)) = LOWER(BTRIM(%s))
            """,
            (id_value,),
        )
    elif id_type == "pubchem_cid":
        pubchem_cid = parse_optional_positive_int(id_value)
        if pubchem_cid is None:
            return None
        cur.execute(
            """
            SELECT compound_id
            FROM compounds
            WHERE pubchem_cid = %s
            """,
            (pubchem_cid,),
        )
    else:
        return None

    row = cur.fetchone()
    return None if row is None else row[0]


def import_compounds_csv(df: pd.DataFrame) -> Dict:
    normalized = df.copy()
    normalized.columns = [str(col).strip().lower() for col in normalized.columns]

    expected_columns = {"a_number", "unii", "pubchem_cid", "chembl_id", "smiles", "common_names"}
    id_columns = {"a_number", "unii", "pubchem_cid", "chembl_id"}

    if not id_columns.intersection(set(normalized.columns)):
        raise ValueError("CSV must include at least one identifier column: a_number, unii, pubchem_cid, chembl_id.")
    unknown = [col for col in normalized.columns if col not in expected_columns]
    if unknown:
        raise ValueError(f"Unexpected columns: {', '.join(unknown)}")
    if normalized.empty:
        raise ValueError("CSV has no data rows.")

    records = normalized.to_dict(orient="records")
    errors: List[Dict] = []
    imported = 0

    with get_conn() as conn, conn.cursor() as cur:
        for row_index, record in enumerate(records, start=2):
            cur.execute("SAVEPOINT csv_row")
            try:
                a_number = clean_text(record.get("a_number"))
                unii = clean_text(record.get("unii"))
                chembl_id = clean_text(record.get("chembl_id"))
                smiles = clean_text(record.get("smiles"))
                pubchem_cid = parse_optional_positive_int(record.get("pubchem_cid"))
                common_names = parse_common_names_field(record.get("common_names"))

                if not any([a_number, unii, pubchem_cid is not None, chembl_id]):
                    raise ValueError("No identifier provided.")

                register_compound_with_cursor(
                    cur=cur,
                    a_number=a_number,
                    unii=unii,
                    pubchem_cid=pubchem_cid,
                    chembl_id=chembl_id,
                    smiles=smiles,
                    common_names=common_names,
                )
                cur.execute("RELEASE SAVEPOINT csv_row")
                imported += 1
            except Exception as exc:
                cur.execute("ROLLBACK TO SAVEPOINT csv_row")
                cur.execute("RELEASE SAVEPOINT csv_row")
                errors.append({"row": row_index, "error": str(exc)})

    return {"total": len(records), "imported": imported, "failed": len(errors), "errors": errors}


def import_ic50_csv(df: pd.DataFrame) -> Dict:
    normalized = df.copy()
    normalized.columns = [str(col).strip().lower() for col in normalized.columns]

    required_columns = {"id_type", "id_value", "ic50_value", "ic50_unit", "qualifier"}
    missing = [col for col in required_columns if col not in normalized.columns]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(sorted(missing))}")

    allowed_columns = required_columns | {"source_ref"}
    unknown = [col for col in normalized.columns if col not in allowed_columns]
    if unknown:
        raise ValueError(f"Unexpected columns: {', '.join(unknown)}")
    if normalized.empty:
        raise ValueError("CSV has no data rows.")

    records = normalized.to_dict(orient="records")
    errors: List[Dict] = []
    imported = 0

    with get_conn() as conn, conn.cursor() as cur:
        for row_index, record in enumerate(records, start=2):
            cur.execute("SAVEPOINT csv_row")
            try:
                id_type = clean_text(record.get("id_type")).lower()
                id_value = clean_text(record.get("id_value"))
                if id_type not in ALLOWED_IDENTIFIER_TYPES:
                    raise ValueError("id_type must be one of: a_number, unii, pubchem_cid, chembl_id.")
                if not id_value:
                    raise ValueError("id_value is required.")

                compound_id = resolve_compound_id_by_identifier(cur=cur, id_type=id_type, id_value=id_value)
                if compound_id is None:
                    raise ValueError(f"No compound found for {id_type}='{id_value}'.")

                ic50_value_text = clean_text(record.get("ic50_value"))
                try:
                    ic50_value = float(ic50_value_text)
                except ValueError as exc:
                    raise ValueError(f"Invalid ic50_value '{ic50_value_text}'.") from exc
                if ic50_value <= 0:
                    raise ValueError("ic50_value must be > 0.")

                ic50_unit = normalize_ic50_unit(record.get("ic50_unit"))
                qualifier = normalize_qualifier(record.get("qualifier"))
                source_ref = clean_text(record.get("source_ref"))

                insert_ic50_result_with_cursor(
                    cur=cur,
                    compound_id=compound_id,
                    ic50_value=ic50_value,
                    ic50_unit=ic50_unit,
                    qualifier=qualifier,
                    source_ref=source_ref,
                )
                cur.execute("RELEASE SAVEPOINT csv_row")
                imported += 1
            except Exception as exc:
                cur.execute("ROLLBACK TO SAVEPOINT csv_row")
                cur.execute("RELEASE SAVEPOINT csv_row")
                errors.append({"row": row_index, "error": str(exc)})

    return {"total": len(records), "imported": imported, "failed": len(errors), "errors": errors}


def fetch_results(limit: int) -> pd.DataFrame:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                r.result_id,
                r.compound_id,
                c.a_number,
                c.unii,
                c.pubchem_cid,
                c.chembl_id,
                c.common_names,
                r.ic50_value,
                r.ic50_unit,
                r.qualifier,
                r.ic50_nm,
                r.pic50,
                r.source_ref,
                r.created_at
            FROM ic50_results r
            JOIN compounds c ON c.compound_id = r.compound_id
            ORDER BY r.created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        columns = [desc.name for desc in cur.description]
    return pd.DataFrame(rows, columns=columns)


def fetch_dashboard_metrics() -> Dict:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM compounds) AS compounds_n,
                (SELECT COUNT(*) FROM ic50_results) AS results_n,
                (SELECT COUNT(DISTINCT compound_id) FROM ic50_results) AS compounds_with_results_n,
                (SELECT MIN(created_at) FROM ic50_results) AS first_result_at,
                (SELECT MAX(created_at) FROM ic50_results) AS last_result_at
            """
        )
        row = cur.fetchone()
    return {
        "compounds_n": row[0] or 0,
        "results_n": row[1] or 0,
        "compounds_with_results_n": row[2] or 0,
        "first_result_at": row[3],
        "last_result_at": row[4],
    }


def fetch_dashboard_data(limit: int) -> pd.DataFrame:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                r.result_id,
                r.compound_id,
                r.ic50_value,
                r.ic50_unit,
                r.qualifier,
                r.ic50_nm,
                r.pic50,
                r.created_at,
                COALESCE(
                    NULLIF(BTRIM(c.chembl_id), ''),
                    NULLIF(BTRIM(c.a_number), ''),
                    NULLIF(BTRIM(c.unii), ''),
                    CASE WHEN c.pubchem_cid IS NULL THEN NULL ELSE 'PUBCHEM:' || c.pubchem_cid::TEXT END,
                    'compound_id:' || c.compound_id::TEXT
                ) AS compound_label
            FROM ic50_results r
            JOIN compounds c ON c.compound_id = r.compound_id
            ORDER BY r.created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        columns = [desc.name for desc in cur.description]
    return pd.DataFrame(rows, columns=columns)


def build_histogram_counts(series: pd.Series, bins: int) -> pd.DataFrame:
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    if numeric.empty:
        return pd.DataFrame(columns=["bin", "count"])

    min_value = float(numeric.min())
    max_value = float(numeric.max())
    if min_value == max_value:
        min_value -= 0.5
        max_value += 0.5

    bucketed = pd.cut(numeric, bins=bins, include_lowest=True)
    counts = bucketed.value_counts(sort=False)
    return pd.DataFrame(
        {
            "bin": [f"{interval.left:.2f} to {interval.right:.2f}" for interval in counts.index],
            "count": counts.values,
        }
    )


def render_import_summary(entity: str, summary: Dict) -> None:
    st.info(
        f"{entity}: imported {summary['imported']} of {summary['total']} rows "
        f"({summary['failed']} failed)."
    )
    if summary["errors"]:
        st.warning("Some rows failed. See details below.")
        st.dataframe(pd.DataFrame(summary["errors"]), use_container_width=True, hide_index=True)


st.title("hERG IC50 Database")
st.write(
    "Use this interface to manually upload results and browse the data."
)

tab1, tab2, tab3, tab4, tab5 = st.tabs(
    ["Add Compound", "Add IC50 Result", "Upload CSV", "Browse Results", "Dashboard"]
)

with tab1:
    st.subheader("Register Compound")
    with st.form("compound_form", clear_on_submit=True):
        a_number = st.text_input("A-number (optional)", max_chars=100)
        unii = st.text_input("UNII (optional)", max_chars=100)
        pubchem_cid_text = st.text_input("PubChem CID (optional)", max_chars=30)
        chembl_id = st.text_input("ChEMBL ID (optional)", max_chars=100)
        smiles = st.text_area("SMILES (optional)", height=100)
        common_names_text = st.text_input("Common names / aliases (comma-separated, optional)")
        submitted = st.form_submit_button("Save Compound")

    if submitted:
        a_number_value = a_number.strip()
        unii_value = unii.strip()
        chembl_value = chembl_id.strip()
        smiles_value = smiles.strip()
        common_names_value = parse_common_names(common_names_text)

        pubchem_cid_value: Optional[int] = None
        input_error = False
        pubchem_text = pubchem_cid_text.strip()
        if pubchem_text:
            try:
                pubchem_cid_value = parse_optional_positive_int(pubchem_text)
            except ValueError:
                st.error("`pubchem_cid` must be a positive integer.")
                input_error = True

        has_identifier = any(
            [
                a_number_value,
                unii_value,
                pubchem_cid_value is not None,
                chembl_value,
            ]
        )

        if not input_error and not has_identifier:
            st.error("Provide at least one identifier: A-number, UNII, PubChem CID, or ChEMBL ID.")
        elif not input_error:
            try:
                compound_id = register_compound(
                    a_number=a_number_value,
                    unii=unii_value,
                    pubchem_cid=pubchem_cid_value,
                    chembl_id=chembl_value,
                    smiles=smiles_value,
                    common_names=common_names_value,
                )
                st.success(f"Compound saved/matched as compound_id={compound_id}.")
            except UniqueViolation:
                st.error("Identifier conflict found while saving compound.")
            except Exception as exc:
                st.error(f"Failed to save compound: {exc}")

with tab2:
    st.subheader("Add IC50 Result")
    compounds = fetch_compounds()
    if not compounds:
        st.info("No compounds found yet. Add at least one compound first.")
    else:
        compound_options = {build_compound_label(c): c["compound_id"] for c in compounds}
        with st.form("result_form", clear_on_submit=True):
            compound_label = st.selectbox("Compound", options=list(compound_options.keys()))
            ic50_value = st.number_input("IC50 Value", min_value=0.000001, value=100.0, step=1.0, format="%.6f")
            ic50_unit = st.selectbox("IC50 Unit", options=["pM", "nM", "uM", "mM"], index=1)
            qualifier = st.selectbox("Qualifier", options=["=", "<", ">"], index=0)
            source_ref = st.text_input("Source Reference (optional)")
            submitted_result = st.form_submit_button("Save IC50 Result")

        if submitted_result:
            try:
                result = insert_ic50_result(
                    compound_id=compound_options[compound_label],
                    ic50_value=float(ic50_value),
                    ic50_unit=ic50_unit,
                    qualifier=qualifier,
                    source_ref=source_ref.strip(),
                )
                st.success(
                    f"Result #{result['result_id']} saved. "
                    f"ic50_nm={result['ic50_nm']}, pIC50={result['pic50']}."
                )
            except Exception as exc:
                st.error(f"Failed to save IC50 result: {exc}")

with tab4:
    st.subheader("Recent Results")
    limit = st.slider("Rows to show", min_value=10, max_value=1000, value=100, step=10)
    try:
        results_df = fetch_results(limit)
        if results_df.empty:
            st.info("No IC50 results found yet.")
        else:
            st.dataframe(results_df, use_container_width=True, hide_index=True)
            st.download_button(
                label="Download CSV",
                data=results_df.to_csv(index=False).encode("utf-8"),
                file_name="ic50_results.csv",
                mime="text/csv",
            )
    except Exception as exc:
        st.error(f"Failed to load results: {exc}")

with tab3:
    st.subheader("Upload CSV")
    st.write("Bulk upload compounds and IC50 results directly from CSV files.")

    compounds_template = (
        "a_number,unii,pubchem_cid,chembl_id,smiles,common_names\n"
        "A-0001,,702,CHEMBL545,CCO,ethanol|ethyl alcohol\n"
        ",RZVAJINKPMORJF-UHFFFAOYSA-N,1983,CHEMBL112,,acetaminophen|paracetamol\n"
    )
    ic50_template = (
        "id_type,id_value,ic50_value,ic50_unit,qualifier,source_ref\n"
        "chembl_id,CHEMBL545,125,nM,=,internal_run_001\n"
        "chembl_id,CHEMBL112,0.85,uM,<,internal_run_001\n"
    )

    st.download_button(
        label="Download compounds CSV template",
        data=compounds_template.encode("utf-8"),
        file_name="compounds_template.csv",
        mime="text/csv",
    )
    st.download_button(
        label="Download IC50 CSV template",
        data=ic50_template.encode("utf-8"),
        file_name="ic50_template.csv",
        mime="text/csv",
    )

    st.markdown("### Import compounds CSV")
    st.caption("Expected columns: a_number, unii, pubchem_cid, chembl_id, smiles, common_names")
    uploaded_compounds = st.file_uploader("Choose compounds CSV", type=["csv"], key="upload_compounds_csv")
    if st.button("Import compounds CSV", key="import_compounds_btn"):
        if uploaded_compounds is None:
            st.error("Please choose a compounds CSV file first.")
        else:
            try:
                uploaded_compounds.seek(0)
                compounds_df = pd.read_csv(uploaded_compounds)
                summary = import_compounds_csv(compounds_df)
                render_import_summary("Compounds", summary)
            except Exception as exc:
                st.error(f"Compounds CSV import failed: {exc}")

    st.markdown("### Import IC50 CSV")
    st.caption("Expected columns: id_type, id_value, ic50_value, ic50_unit, qualifier, source_ref")
    uploaded_ic50 = st.file_uploader("Choose IC50 CSV", type=["csv"], key="upload_ic50_csv")
    if st.button("Import IC50 CSV", key="import_ic50_btn"):
        if uploaded_ic50 is None:
            st.error("Please choose an IC50 CSV file first.")
        else:
            try:
                uploaded_ic50.seek(0)
                ic50_df = pd.read_csv(uploaded_ic50)
                summary = import_ic50_csv(ic50_df)
                render_import_summary("IC50 results", summary)
            except Exception as exc:
                st.error(f"IC50 CSV import failed: {exc}")

with tab5:
    st.subheader("Data Dashboard")
    st.write("Summary metrics and distribution views for loaded IC50 records.")

    try:
        metrics = fetch_dashboard_metrics()
    except Exception as exc:
        st.error(f"Failed to load dashboard metrics: {exc}")
        metrics = None

    if metrics is not None:
        metric_col1, metric_col2, metric_col3, metric_col4, metric_col5 = st.columns(5)
        metric_col1.metric("Compounds", f"{metrics['compounds_n']:,}")
        metric_col2.metric("IC50 Entries", f"{metrics['results_n']:,}")
        metric_col3.metric("Compounds With Results", f"{metrics['compounds_with_results_n']:,}")
        metric_col4.metric(
            "First Entry",
            metrics["first_result_at"].strftime("%Y-%m-%d") if metrics["first_result_at"] else "-",
        )
        metric_col5.metric(
            "Latest Entry",
            metrics["last_result_at"].strftime("%Y-%m-%d") if metrics["last_result_at"] else "-",
        )

    max_rows = st.slider(
        "Rows to analyze",
        min_value=100,
        max_value=250000,
        value=20000,
        step=100,
        help="Recent rows to pull from the database for visualization.",
    )

    try:
        dashboard_df = fetch_dashboard_data(limit=max_rows)
    except Exception as exc:
        st.error(f"Failed to load dashboard data: {exc}")
        dashboard_df = pd.DataFrame()

    if dashboard_df.empty:
        st.info("No IC50 data available yet.")
    else:
        dashboard_df["created_at"] = pd.to_datetime(dashboard_df["created_at"], errors="coerce")
        dashboard_df["ic50_nm"] = pd.to_numeric(dashboard_df["ic50_nm"], errors="coerce")
        dashboard_df["pic50"] = pd.to_numeric(dashboard_df["pic50"], errors="coerce")
        dashboard_df["log10_ic50_nm"] = dashboard_df["ic50_nm"].apply(
            lambda value: math.log10(value) if pd.notna(value) and value > 0 else None
        )

        st.markdown("### Filters")
        filter_col1, filter_col2 = st.columns(2)
        with filter_col1:
            qualifier_options = sorted(dashboard_df["qualifier"].dropna().astype(str).unique().tolist())
            selected_qualifiers = st.multiselect(
                "Qualifier",
                options=qualifier_options,
                default=qualifier_options,
            )
        with filter_col2:
            unit_options = sorted(dashboard_df["ic50_unit"].dropna().astype(str).unique().tolist())
            selected_units = st.multiselect(
                "Unit",
                options=unit_options,
                default=unit_options,
            )

        filtered_df = dashboard_df.copy()
        if selected_qualifiers:
            filtered_df = filtered_df[filtered_df["qualifier"].isin(selected_qualifiers)]
        if selected_units:
            filtered_df = filtered_df[filtered_df["ic50_unit"].isin(selected_units)]

        st.caption(f"Visualizing {len(filtered_df):,} rows after filters.")

        if filtered_df.empty:
            st.info("No data left after filtering.")
        else:
            st.markdown("### Category Distributions")
            dist_col1, dist_col2 = st.columns(2)
            with dist_col1:
                st.caption("Qualifier counts")
                qualifier_counts = (
                    filtered_df["qualifier"]
                    .astype(str)
                    .value_counts()
                    .reindex(["=", "<", ">"], fill_value=0)
                    .reset_index()
                )
                qualifier_counts.columns = ["qualifier", "count"]
                st.bar_chart(qualifier_counts.set_index("qualifier"))
            with dist_col2:
                st.caption("Unit counts")
                unit_counts = (
                    filtered_df["ic50_unit"]
                    .astype(str)
                    .value_counts()
                    .sort_index()
                    .reset_index()
                )
                unit_counts.columns = ["unit", "count"]
                st.bar_chart(unit_counts.set_index("unit"))

            st.markdown("### Value Distributions")
            value_col1, value_col2 = st.columns(2)
            with value_col1:
                st.caption("pIC50 histogram")
                pic50_hist = build_histogram_counts(filtered_df["pic50"], bins=30)
                if pic50_hist.empty:
                    st.info("No valid pIC50 values.")
                else:
                    st.bar_chart(pic50_hist.set_index("bin"))
            with value_col2:
                st.caption("log10(IC50 nM) histogram")
                ic50_log_hist = build_histogram_counts(filtered_df["log10_ic50_nm"], bins=30)
                if ic50_log_hist.empty:
                    st.info("No valid IC50 values.")
                else:
                    st.bar_chart(ic50_log_hist.set_index("bin"))

            st.markdown("### Trend and Top Compounds")
            trend_col1, trend_col2 = st.columns(2)
            with trend_col1:
                st.caption("Entries per month")
                monthly_counts = (
                    filtered_df.dropna(subset=["created_at"])
                    .assign(month=lambda df: df["created_at"].dt.to_period("M").astype(str))
                    .groupby("month")
                    .size()
                    .reset_index(name="count")
                    .sort_values("month")
                )
                if monthly_counts.empty:
                    st.info("No valid timestamps for trend plot.")
                else:
                    st.line_chart(monthly_counts.set_index("month"))
            with trend_col2:
                st.caption("Top compounds by entry count")
                top_compounds = (
                    filtered_df["compound_label"]
                    .astype(str)
                    .value_counts()
                    .head(15)
                    .reset_index()
                )
                top_compounds.columns = ["compound", "count"]
                st.bar_chart(top_compounds.set_index("compound"))
