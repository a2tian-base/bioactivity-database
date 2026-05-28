from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pandas as pd
import streamlit as st
from psycopg.errors import UniqueViolation

from bioactivity.db import upsert_bioactivity_result
from bioactivity.endpoints import EndpointConfig, list_active_endpoints, load_endpoint
from bioactivity.models import MeasurementInput, measurement_from_ic50
from bioactivity.results import (
    count_bioactivity_results,
    fetch_bioactivity_results,
    format_bioactivity_result_row,
    manual_entry_schema,
)
from herg.db import get_conn, upsert_compound, upsert_ic50_result, upsert_source_record
from herg.models import CompoundInput, Ic50Input, SourceRecordInput
from herg.normalize import (
    build_identifier_inputs,
    build_name_inputs,
    clean_text,
    normalize_ic50_unit,
    normalize_qualifier,
    parse_optional_positive_int,
    parse_pipe_or_comma_names,
)
from herg.read_db import fetch_compounds, resolve_compound_id


CONCENTRATION_UNIT_TO_UM_FACTOR = {
    "pM": Decimal("0.000001"),
    "nM": Decimal("0.001"),
    "uM": Decimal("1"),
    "mM": Decimal("1000"),
}


def build_compound_label(compound: dict[str, Any]) -> str:
    compound_id = compound.get("compound_id")
    preferred_name = clean_text(compound.get("preferred_name"))
    chembl_id = clean_text(compound.get("chembl_id"))
    a_number = clean_text(compound.get("a_number"))
    unii = clean_text(compound.get("unii"))
    pubchem_cid = compound.get("pubchem_cid")
    standard_inchikey = clean_text(compound.get("standard_inchikey"))

    if preferred_name:
        label = preferred_name
    elif chembl_id:
        label = f"ChEMBL:{chembl_id}"
    elif a_number:
        label = f"A-number:{a_number}"
    elif unii:
        label = f"UNII:{unii}"
    elif pubchem_cid:
        label = f"PubChem:{pubchem_cid}"
    else:
        label = f"compound_id:{compound_id}"

    if standard_inchikey:
        label = f"{label} | InChIKey:{standard_inchikey}"

    return f"{label} (id={compound_id})"


def endpoint_label(endpoint: EndpointConfig) -> str:
    return f"{endpoint.display_name} ({endpoint.endpoint_key})"


def load_active_endpoint_options() -> list[EndpointConfig]:
    with get_conn() as conn:
        return list_active_endpoints(conn)


def _default_endpoint_index(endpoints: list[EndpointConfig]) -> int:
    for index, endpoint in enumerate(endpoints):
        if endpoint.endpoint_key == "herg_ic50":
            return index
    return 0


def import_compounds_csv(df: pd.DataFrame) -> dict[str, Any]:
    normalized = df.copy()
    normalized.columns = [str(col).strip().lower() for col in normalized.columns]

    expected_columns = {
        "a_number",
        "unii",
        "pubchem_cid",
        "chembl_id",
        "standard_inchikey",
        "standard_inchi",
        "canonical_smiles",
        "preferred_name",
        "common_names",
    }

    unknown = [col for col in normalized.columns if col not in expected_columns]
    if unknown:
        raise ValueError(f"Unexpected columns: {', '.join(unknown)}")
    if normalized.empty:
        raise ValueError("CSV has no data rows.")

    records = normalized.to_dict(orient="records")
    errors: list[dict[str, Any]] = []
    imported = 0

    with get_conn() as conn, conn.cursor() as cur:
        for row_index, record in enumerate(records, start=2):
            cur.execute("SAVEPOINT csv_row")
            try:
                a_number = clean_text(record.get("a_number"))
                unii = clean_text(record.get("unii"))
                chembl_id = clean_text(record.get("chembl_id"))
                standard_inchikey = clean_text(record.get("standard_inchikey"))
                standard_inchi = clean_text(record.get("standard_inchi"))
                canonical_smiles = clean_text(record.get("canonical_smiles"))
                preferred_name = clean_text(record.get("preferred_name"))
                aliases = parse_pipe_or_comma_names(record.get("common_names"))

                pubchem_cid_value: int | None = None
                pubchem_text = clean_text(record.get("pubchem_cid"))
                if pubchem_text:
                    pubchem_cid_value = parse_optional_positive_int(pubchem_text)

                identifier_values = {
                    "a_number": a_number,
                    "unii": unii,
                    "pubchem_cid": str(pubchem_cid_value) if pubchem_cid_value is not None else "",
                    "chembl_id": chembl_id,
                }
                identifiers = build_identifier_inputs(identifier_values)
                names = build_name_inputs(preferred_name=preferred_name, aliases=aliases)

                if not identifiers and not standard_inchikey:
                    raise ValueError("No identifier or Standard InChIKey provided.")

                compound_input = CompoundInput(
                    canonical_smiles=canonical_smiles,
                    standard_inchi=standard_inchi,
                    standard_inchikey=standard_inchikey,
                    identifiers=identifiers,
                    names=names,
                )

                upsert_compound(cur, compound_input)
                cur.execute("RELEASE SAVEPOINT csv_row")
                imported += 1
            except Exception as exc:
                cur.execute("ROLLBACK TO SAVEPOINT csv_row")
                cur.execute("RELEASE SAVEPOINT csv_row")
                errors.append({"row": row_index, "error": str(exc)})

    return {"total": len(records), "imported": imported, "failed": len(errors), "errors": errors}


def _upsert_herg_bioactivity_result(
    cur: Any,
    *,
    endpoint: EndpointConfig,
    compound_id: int,
    source_record_id: int,
    source_record_key: str,
    measurement: Ic50Input,
    entry_path: str,
) -> tuple[int, dict[str, Any]]:
    legacy_result = upsert_ic50_result(
        cur=cur,
        compound_id=compound_id,
        source_record_id=source_record_id,
        measurement=measurement,
    )
    bioactivity_result_id = upsert_bioactivity_result(
        cur,
        endpoint_id=endpoint.endpoint_id,
        compound_id=compound_id,
        source_record_id=source_record_id,
        ingestion_run_id=None,
        measurement=measurement_from_ic50(
            result_key=source_record_key,
            ic50_value=measurement.ic50_value,
            ic50_unit=measurement.ic50_unit,
            qualifier=measurement.qualifier,
            ic50_um=legacy_result["ic50_um"],
            pic50=legacy_result["pic50"],
            pic50_qualifier=legacy_result["pic50_qualifier"],
            assay_context={"entry_path": entry_path},
            quality_flags={"entry_path": entry_path},
        ),
    )
    return bioactivity_result_id, legacy_result


def import_ic50_csv(df: pd.DataFrame) -> dict[str, Any]:
    normalized = df.copy()
    normalized.columns = [str(col).strip().lower() for col in normalized.columns]

    required_columns = {
        "id_type",
        "id_value",
        "ic50_value",
        "ic50_unit",
        "qualifier",
        "source_name",
        "source_record_key",
        "source_release",
        "source_url",
    }
    missing = [col for col in required_columns if col not in normalized.columns]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(sorted(missing))}")

    unknown = [col for col in normalized.columns if col not in required_columns]
    if unknown:
        raise ValueError(f"Unexpected columns: {', '.join(unknown)}")
    if normalized.empty:
        raise ValueError("CSV has no data rows.")

    records = normalized.to_dict(orient="records")
    errors: list[dict[str, Any]] = []
    imported = 0

    with get_conn() as conn, conn.cursor() as cur:
        herg_endpoint = load_endpoint(cur, "herg_ic50")
        for row_index, record in enumerate(records, start=2):
            cur.execute("SAVEPOINT csv_row")
            try:
                id_type = clean_text(record.get("id_type")).lower()
                id_value = clean_text(record.get("id_value"))
                if not id_type:
                    raise ValueError("id_type is required.")
                if not id_value:
                    raise ValueError("id_value is required.")

                compound_id = resolve_compound_id(cur=cur, id_type=id_type, id_value=id_value)
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

                source_name = clean_text(record.get("source_name"))
                source_record_key = clean_text(record.get("source_record_key"))
                source_release = clean_text(record.get("source_release"))
                source_url = clean_text(record.get("source_url"))

                if not source_name:
                    raise ValueError("source_name is required.")
                if not source_record_key:
                    raise ValueError("source_record_key is required.")

                source_input = SourceRecordInput(
                    source_name=source_name,
                    source_record_key=source_record_key,
                    record_type="csv_import",
                    source_release=source_release,
                    source_url=source_url,
                )
                source_record_id = upsert_source_record(cur, source_input)

                measurement = Ic50Input(
                    ic50_value=ic50_value,
                    ic50_unit=ic50_unit,
                    qualifier=qualifier,
                    endpoint="IC50",
                )
                _upsert_herg_bioactivity_result(
                    cur,
                    endpoint=herg_endpoint,
                    compound_id=compound_id,
                    source_record_id=source_record_id,
                    source_record_key=source_record_key,
                    measurement=measurement,
                    entry_path="streamlit_csv_import",
                )
                cur.execute("RELEASE SAVEPOINT csv_row")
                imported += 1
            except Exception as exc:
                cur.execute("ROLLBACK TO SAVEPOINT csv_row")
                cur.execute("RELEASE SAVEPOINT csv_row")
                errors.append({"row": row_index, "error": str(exc)})

    return {"total": len(records), "imported": imported, "failed": len(errors), "errors": errors}


def build_histogram_counts(series: pd.Series, bins: int) -> pd.DataFrame:
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    if numeric.empty:
        return pd.DataFrame(columns=["bin_start", "bin_end", "bin", "count"])

    min_value = float(numeric.min())
    max_value = float(numeric.max())
    if min_value == max_value:
        min_value -= 0.5
        max_value += 0.5

    bucketed = pd.cut(numeric, bins=bins, include_lowest=True)
    counts = bucketed.value_counts(sort=False)
    return pd.DataFrame(
        {
            "bin_start": [float(interval.left) for interval in counts.index],
            "bin_end": [float(interval.right) for interval in counts.index],
            "bin": [f"{interval.left:.2f} to {interval.right:.2f}" for interval in counts.index],
            "count": counts.values,
        }
    )


def render_import_summary(entity: str, summary: dict[str, Any]) -> None:
    st.info(
        f"{entity}: imported {summary['imported']} of {summary['total']} rows "
        f"({summary['failed']} failed)."
    )
    if summary["errors"]:
        st.warning("Some rows failed. See details below.")
        st.dataframe(pd.DataFrame(summary["errors"]), use_container_width=True, hide_index=True)


def _format_results_dataframe(rows: list[dict[str, Any]]) -> pd.DataFrame:
    formatted_rows = [format_bioactivity_result_row(row) for row in rows]
    return pd.DataFrame(formatted_rows)


def _format_legacy_ic50_results_dataframe(results_df: pd.DataFrame) -> pd.DataFrame:
    formatted_rows: list[dict[str, Any]] = []
    for row in results_df.to_dict(orient="records"):
        standard_value = row.get("ic50_um")
        standard_relation = row.get("qualifier") if standard_value is not None else None
        formatted_rows.append(
            {
                "result_id": row.get("result_id"),
                "compound_id": row.get("compound_id"),
                "compound": row.get("compound_label") or row.get("compound_id"),
                "source": row.get("source_name"),
                "source_record_key": row.get("source_record_key"),
                "measurement_type": "IC50",
                "value_kind": "concentration",
                "measurement": format_bioactivity_result_row(
                    {
                        "measurement_type": "IC50",
                        "value_kind": "concentration",
                        "standard_value": standard_value,
                        "standard_unit": "uM" if standard_value is not None else None,
                        "standard_relation": standard_relation,
                        "original_value": row.get("ic50_value"),
                        "original_unit": row.get("ic50_unit"),
                        "original_relation": row.get("qualifier"),
                    }
                )["measurement"],
                "original_value": row.get("ic50_value"),
                "original_unit": row.get("ic50_unit"),
                "original_relation": row.get("qualifier"),
                "standard_value": standard_value,
                "standard_unit": "uM" if standard_value is not None else "",
                "standard_relation": standard_relation or "",
                "p_value": row.get("pic50"),
                "p_value_relation": row.get("pic50_qualifier"),
                "p_value_display": format_bioactivity_result_row(
                    {
                        "measurement_type": "IC50",
                        "value_kind": "concentration",
                        "p_value": row.get("pic50"),
                        "p_value_relation": row.get("pic50_qualifier"),
                    }
                )["p_value_display"],
                "value_text": "",
                "assay_context": "",
                "quality_flags": "",
                "endpoint_id": None,
                "endpoint_key": "herg_ic50",
                "endpoint": "hERG IC50",
                "source_record_id": row.get("source_record_id"),
                "ingestion_run_id": None,
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }
        )
    return pd.DataFrame(formatted_rows)


def _count_legacy_ic50_results_without_bioactivity(endpoint_id: int) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM ic50_result_summary_v r
            WHERE NOT EXISTS (
                SELECT 1
                FROM bioactivity_results br
                WHERE br.endpoint_id = %s
                  AND br.source_record_id = r.source_record_id
            )
            """,
            (endpoint_id,),
        )
        row = cur.fetchone()
    return int(row[0] or 0)


def _fetch_legacy_ic50_results_without_bioactivity(endpoint_id: int, limit: int | None) -> pd.DataFrame:
    query = """
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
            r.source_name,
            r.source_record_key,
            r.source_release,
            r.source_url,
            r.preferred_name,
            r.a_number,
            r.unii,
            r.pubchem_cid,
            r.chembl_id,
            r.standard_inchikey,
            c.canonical_smiles AS compound_smiles,
            r.compound_label
        FROM ic50_result_summary_v r
        JOIN compound_summary_v c
          ON c.compound_id = r.compound_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM bioactivity_results br
            WHERE br.endpoint_id = %s
              AND br.source_record_id = r.source_record_id
        )
        ORDER BY r.created_at DESC, r.result_id DESC
    """
    params: tuple[Any, ...] = (endpoint_id,)
    if limit is not None:
        query += "\nLIMIT %s"
        params = (endpoint_id, limit)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
        columns = [desc.name for desc in cur.description]
    return pd.DataFrame(rows, columns=columns)


def _combine_result_dataframes(
    bioactivity_df: pd.DataFrame,
    legacy_df: pd.DataFrame,
    limit: int | None,
) -> pd.DataFrame:
    if bioactivity_df.empty:
        combined_df = legacy_df.copy()
    elif legacy_df.empty:
        combined_df = bioactivity_df.copy()
    else:
        combined_df = pd.concat([bioactivity_df, legacy_df], ignore_index=True, sort=False)

    if combined_df.empty:
        return combined_df

    sorted_df = combined_df.copy()
    sorted_df["_created_at_sort"] = pd.to_datetime(sorted_df["created_at"], errors="coerce")
    sorted_df["_result_id_sort"] = pd.to_numeric(sorted_df["result_id"], errors="coerce")
    sorted_df = sorted_df.sort_values(
        by=["_created_at_sort", "_result_id_sort"],
        ascending=[False, False],
        na_position="last",
        kind="mergesort",
    ).drop(columns=["_created_at_sort", "_result_id_sort"])
    if limit is not None:
        sorted_df = sorted_df.head(limit)
    return sorted_df.reset_index(drop=True)


def _load_results_for_endpoint(selected_endpoint: EndpointConfig, limit: int | None) -> tuple[int, pd.DataFrame, str]:
    with get_conn() as conn:
        total_results = count_bioactivity_results(conn, selected_endpoint.endpoint_id)
        rows = fetch_bioactivity_results(conn, endpoint_id=selected_endpoint.endpoint_id, limit=limit)
    results_df = _format_results_dataframe(rows)

    # During the migration, existing hERG rows may live only in ic50_results.
    # Keep that compatibility source explicit, and exclude rows already
    # represented in bioactivity_results to avoid duplicating dual-written rows.
    if selected_endpoint.endpoint_key == "herg_ic50":
        legacy_total = _count_legacy_ic50_results_without_bioactivity(selected_endpoint.endpoint_id)
        if legacy_total > 0:
            legacy_df = _format_legacy_ic50_results_dataframe(
                _fetch_legacy_ic50_results_without_bioactivity(selected_endpoint.endpoint_id, limit)
            )
            combined_df = _combine_result_dataframes(results_df, legacy_df, limit)
            if total_results > 0:
                return total_results + legacy_total, combined_df, "bioactivity_results + legacy ic50_results"
            return legacy_total, combined_df, "legacy ic50_results"

    return total_results, results_df, "bioactivity_results"


def _source_record_from_form(
    *,
    source_name: str,
    source_record_key: str,
    source_release: str,
    source_url: str,
    record_type: str,
) -> SourceRecordInput:
    source_name_value = clean_text(source_name)
    if not source_name_value:
        raise ValueError("source_name is required.")

    source_record_key_value = clean_text(source_record_key)
    if not source_record_key_value:
        source_record_key_value = f"manual:{uuid.uuid4()}"

    return SourceRecordInput(
        source_name=source_name_value,
        source_record_key=source_record_key_value,
        record_type=record_type,
        source_release=clean_text(source_release),
        source_url=clean_text(source_url),
    )


def _normalize_concentration_unit(unit: str) -> str:
    raw = clean_text(unit).replace("\u00b5", "u").replace("\u03bc", "u").replace(" ", "")
    mapping = {
        "pm": "pM",
        "nm": "nM",
        "um": "uM",
        "mm": "mM",
    }
    return mapping.get(raw.lower(), raw)


def _standardize_concentration_value(value: float, unit: str, canonical_unit: str | None) -> Decimal | None:
    if not canonical_unit:
        return None

    normalized_unit = _normalize_concentration_unit(unit)
    normalized_canonical_unit = _normalize_concentration_unit(canonical_unit)
    value_decimal = Decimal(str(value))
    if normalized_unit == normalized_canonical_unit:
        return value_decimal

    if normalized_unit in CONCENTRATION_UNIT_TO_UM_FACTOR and normalized_canonical_unit in CONCENTRATION_UNIT_TO_UM_FACTOR:
        value_um = value_decimal * CONCENTRATION_UNIT_TO_UM_FACTOR[normalized_unit]
        return value_um / CONCENTRATION_UNIT_TO_UM_FACTOR[normalized_canonical_unit]

    raise ValueError(
        f"Cannot normalize concentration unit '{unit}' to canonical unit '{canonical_unit}'. "
        f"Enter values in {canonical_unit} for this endpoint."
    )


def _measurement_from_concentration_form(
    *,
    source_record_key: str,
    measurement_type: str,
    value: float,
    unit: str,
    relation: str,
    canonical_unit: str | None,
) -> MeasurementInput:
    normalized_unit = _normalize_concentration_unit(unit)
    normalized_canonical_unit = _normalize_concentration_unit(canonical_unit) if canonical_unit else None
    standard_value = _standardize_concentration_value(value, normalized_unit, normalized_canonical_unit)
    return MeasurementInput(
        result_key=source_record_key,
        measurement_type=measurement_type,
        value_kind="concentration",
        original_value=value,
        original_unit=normalized_unit,
        original_relation=relation,
        standard_value=standard_value,
        standard_unit=normalized_canonical_unit if standard_value is not None else None,
        standard_relation=relation if standard_value is not None else None,
        assay_context={"entry_path": "streamlit_manual_entry"},
        quality_flags={"entry_path": "streamlit_manual_entry"},
    )


def render_compound_tab() -> None:
    st.subheader("Register Compound")
    with st.form("compound_form", clear_on_submit=True):
        a_number = st.text_input("A-number (optional)", max_chars=100)
        unii = st.text_input("UNII (optional)", max_chars=100)
        pubchem_cid_text = st.text_input("PubChem CID (optional)", max_chars=30)
        chembl_id = st.text_input("ChEMBL ID (optional)", max_chars=100)
        standard_inchikey = st.text_input("Standard InChIKey (optional)", max_chars=200)
        standard_inchi = st.text_area("Standard InChI (optional)", height=80)
        canonical_smiles = st.text_area("Canonical SMILES (optional)", height=80)
        preferred_name = st.text_input("Preferred name (optional)", max_chars=200)
        aliases_text = st.text_input("Aliases (pipe or comma-separated, optional)")
        submitted = st.form_submit_button("Save Compound")

    if submitted:
        a_number_value = clean_text(a_number)
        unii_value = clean_text(unii)
        chembl_value = clean_text(chembl_id)
        standard_inchikey_value = clean_text(standard_inchikey)
        standard_inchi_value = clean_text(standard_inchi)
        canonical_smiles_value = clean_text(canonical_smiles)
        preferred_name_value = clean_text(preferred_name)
        aliases_value = parse_pipe_or_comma_names(aliases_text)

        pubchem_cid_value: int | None = None
        input_error = False
        pubchem_text = clean_text(pubchem_cid_text)
        if pubchem_text:
            try:
                pubchem_cid_value = parse_optional_positive_int(pubchem_text)
            except ValueError:
                st.error("`pubchem_cid` must be a positive integer.")
                input_error = True

        identifier_values = {
            "a_number": a_number_value,
            "unii": unii_value,
            "pubchem_cid": str(pubchem_cid_value) if pubchem_cid_value is not None else "",
            "chembl_id": chembl_value,
        }
        identifiers = build_identifier_inputs(identifier_values)
        names = build_name_inputs(preferred_name=preferred_name_value, aliases=aliases_value)

        if not input_error and not identifiers and not standard_inchikey_value:
            st.error("Provide at least one identifier or a Standard InChIKey.")
        elif not input_error:
            compound_input = CompoundInput(
                canonical_smiles=canonical_smiles_value,
                standard_inchi=standard_inchi_value,
                standard_inchikey=standard_inchikey_value,
                identifiers=identifiers,
                names=names,
            )

            try:
                with get_conn() as conn, conn.cursor() as cur:
                    compound_id = upsert_compound(cur, compound_input)
                st.success(f"Compound saved/matched as compound_id={compound_id}.")
            except UniqueViolation:
                st.error("Identifier conflict found while saving compound.")
            except Exception as exc:
                st.error(f"Failed to save compound: {exc}")


def render_measurement_tab(selected_endpoint: EndpointConfig) -> None:
    st.subheader("Add Bioactivity Measurement")
    st.caption(f"Endpoint: {endpoint_label(selected_endpoint)}")

    schema = manual_entry_schema(selected_endpoint)
    if not schema.supported:
        st.info(schema.message)
        return

    compounds = fetch_compounds()
    if not compounds:
        st.info("No compounds found yet. Add at least one compound first.")
        return

    compound_options = {build_compound_label(c): c["compound_id"] for c in compounds}
    unit_options = schema.allowed_units or ([schema.canonical_unit] if schema.canonical_unit else ["uM"])
    relation_options = schema.allowed_relations or ["=", "<", ">"]
    unit_index = unit_options.index(schema.canonical_unit) if schema.canonical_unit in unit_options else 0
    measurement_label = schema.measurement_type or selected_endpoint.display_name

    with st.form("result_form", clear_on_submit=True):
        compound_label = st.selectbox("Compound", options=list(compound_options.keys()))
        measurement_value = st.number_input(
            f"{measurement_label} value",
            min_value=0.000001,
            value=100.0,
            step=1.0,
            format="%.6f",
        )
        measurement_unit = st.selectbox(f"{measurement_label} unit", options=unit_options, index=unit_index)
        relation = st.selectbox("Relation", options=relation_options, index=0)
        source_name = st.text_input("Source name", value="manual")
        source_record_key = st.text_input("Source record key (optional)")
        source_release = st.text_input("Source release (optional)")
        source_url = st.text_input("Source URL (optional)")
        submitted_result = st.form_submit_button("Save Measurement")

    if not submitted_result:
        return

    try:
        source_input = _source_record_from_form(
            source_name=source_name,
            source_record_key=source_record_key,
            source_release=source_release,
            source_url=source_url,
            record_type="manual_entry",
        )
        compound_id = int(compound_options[compound_label])

        with get_conn() as conn, conn.cursor() as cur:
            source_record_id = upsert_source_record(cur, source_input)
            if selected_endpoint.endpoint_key == "herg_ic50":
                measurement = Ic50Input(
                    ic50_value=float(measurement_value),
                    ic50_unit=normalize_ic50_unit(measurement_unit),
                    qualifier=normalize_qualifier(relation),
                    endpoint="IC50",
                )
                bioactivity_result_id, legacy_result = _upsert_herg_bioactivity_result(
                    cur,
                    endpoint=selected_endpoint,
                    compound_id=compound_id,
                    source_record_id=source_record_id,
                    source_record_key=source_input.source_record_key,
                    measurement=measurement,
                    entry_path="streamlit_manual_entry",
                )
                st.success(
                    f"Result #{bioactivity_result_id} saved. "
                    f"IC50={legacy_result['ic50_um']} uM, pIC50={legacy_result['pic50']} "
                    f"(relation {legacy_result['pic50_qualifier']})."
                )
            else:
                measurement = _measurement_from_concentration_form(
                    source_record_key=source_input.source_record_key,
                    measurement_type=schema.measurement_type,
                    value=float(measurement_value),
                    unit=measurement_unit,
                    relation=relation,
                    canonical_unit=schema.canonical_unit,
                )
                bioactivity_result_id = upsert_bioactivity_result(
                    cur,
                    endpoint_id=selected_endpoint.endpoint_id,
                    compound_id=compound_id,
                    source_record_id=source_record_id,
                    ingestion_run_id=None,
                    measurement=measurement,
                )
                st.success(f"Result #{bioactivity_result_id} saved for {selected_endpoint.display_name}.")
    except Exception as exc:
        st.error(f"Failed to save measurement: {exc}")


def render_upload_tab(selected_endpoint: EndpointConfig) -> None:
    st.subheader("Upload CSV")
    st.write("Bulk upload compounds and hERG IC50 result rows directly from CSV files.")

    compounds_template = (
        "a_number,unii,pubchem_cid,chembl_id,standard_inchikey,standard_inchi,canonical_smiles,preferred_name,common_names\n"
        "A-0001,,702,CHEMBL545,LFQSCWFLJHTTHZ-UHFFFAOYSA-N,,CCO,ethanol,ethyl alcohol|alcohol\n"
    )
    ic50_template = (
        "id_type,id_value,ic50_value,ic50_unit,qualifier,source_name,source_record_key,source_release,source_url\n"
        "chembl_id,CHEMBL545,125,nM,=,manual,manual:001,,\n"
        "pubchem_cid,1983,0.85,uM,<,literature,paper:smith-2024-table2,,\n"
    )

    st.download_button(
        label="Download compounds CSV template",
        data=compounds_template.encode("utf-8"),
        file_name="compounds_template.csv",
        mime="text/csv",
    )

    st.markdown("### Import compounds CSV")
    st.caption(
        "Expected columns: a_number, unii, pubchem_cid, chembl_id, standard_inchikey, "
        "standard_inchi, canonical_smiles, preferred_name, common_names"
    )
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

    st.markdown("### Import hERG IC50 CSV")
    st.info("Result CSV import is currently limited to the hERG IC50 endpoint.")
    if selected_endpoint.endpoint_key != "herg_ic50":
        st.caption("Select hERG IC50 to import result CSV rows.")
        return

    st.download_button(
        label="Download hERG IC50 CSV template",
        data=ic50_template.encode("utf-8"),
        file_name="herg_ic50_template.csv",
        mime="text/csv",
    )
    st.caption(
        "Expected columns: id_type, id_value, ic50_value, ic50_unit, qualifier, "
        "source_name, source_record_key, source_release, source_url"
    )
    uploaded_ic50 = st.file_uploader("Choose hERG IC50 CSV", type=["csv"], key="upload_ic50_csv")
    if st.button("Import hERG IC50 CSV", key="import_ic50_btn"):
        if uploaded_ic50 is None:
            st.error("Please choose a hERG IC50 CSV file first.")
        else:
            try:
                uploaded_ic50.seek(0)
                ic50_df = pd.read_csv(uploaded_ic50)
                summary = import_ic50_csv(ic50_df)
                render_import_summary("hERG IC50 results", summary)
            except Exception as exc:
                st.error(f"hERG IC50 CSV import failed: {exc}")


def render_browse_tab(selected_endpoint: EndpointConfig) -> None:
    st.subheader("Browse Results")
    st.caption(f"Endpoint: {endpoint_label(selected_endpoint)}")
    limit = st.slider("Rows to preview", min_value=10, max_value=1000, value=100, step=10)

    try:
        total_results, results_df, data_source = _load_results_for_endpoint(selected_endpoint, limit)
        if total_results == 0 or results_df.empty:
            st.info("No normalized bioactivity results found for this endpoint.")
        else:
            st.caption(
                f"Previewing {len(results_df):,} of {total_results:,} {data_source} rows. "
                "The on-screen table stays capped for performance."
            )
            st.dataframe(results_df, use_container_width=True, hide_index=True)
            st.download_button(
                label="Download preview CSV",
                data=results_df.to_csv(index=False).encode("utf-8"),
                file_name=f"{selected_endpoint.endpoint_key}_bioactivity_results_preview.csv",
                mime="text/csv",
            )

            prepare_full_export = st.checkbox(
                "Prepare full CSV export",
                help="Load every row for the selected endpoint and make it available as a CSV download.",
            )
            if prepare_full_export:
                with st.spinner("Preparing full results export..."):
                    _, full_results_df, full_data_source = _load_results_for_endpoint(selected_endpoint, None)
                st.caption(f"Full export contains {len(full_results_df):,} rows.")
                st.download_button(
                    label=f"Download full {full_data_source} CSV",
                    data=full_results_df.to_csv(index=False).encode("utf-8"),
                    file_name=f"{selected_endpoint.endpoint_key}_bioactivity_results_all.csv",
                    mime="text/csv",
                )
    except Exception as exc:
        st.error(f"Failed to load results: {exc}")


def render_dashboard_tab(selected_endpoint: EndpointConfig) -> None:
    st.subheader("Data Dashboard")
    st.caption(f"Endpoint: {endpoint_label(selected_endpoint)}")

    max_rows = st.slider(
        "Rows to analyze",
        min_value=100,
        max_value=250000,
        value=20000,
        step=100,
        help="Recent rows to pull from bioactivity_results for visualization.",
    )

    try:
        total_results, dashboard_df, data_source = _load_results_for_endpoint(selected_endpoint, max_rows)
    except Exception as exc:
        st.error(f"Failed to load dashboard data: {exc}")
        total_results = 0
        dashboard_df = pd.DataFrame()
        data_source = "bioactivity_results"

    if dashboard_df.empty:
        st.info("No normalized bioactivity data available for this endpoint yet.")
        return

    st.caption(f"Dashboard source: {data_source}.")

    metric_col1, metric_col2, metric_col3, metric_col4, metric_col5 = st.columns(5)
    metric_col1.metric("Results", f"{total_results:,}")
    metric_col2.metric("Loaded Rows", f"{len(dashboard_df):,}")
    metric_col3.metric("Compounds", f"{dashboard_df['compound_id'].nunique():,}")
    metric_col4.metric("Sources", f"{dashboard_df['source'].nunique():,}")
    metric_col5.metric("Value Kinds", f"{dashboard_df['value_kind'].nunique():,}")

    dashboard_df["created_at"] = pd.to_datetime(dashboard_df["created_at"], errors="coerce")
    dashboard_df["standard_value_numeric"] = pd.to_numeric(dashboard_df["standard_value"], errors="coerce")
    dashboard_df["p_value_numeric"] = pd.to_numeric(dashboard_df["p_value"], errors="coerce")

    st.markdown("### Filters")
    filter_col1, filter_col2 = st.columns(2)
    with filter_col1:
        source_options = sorted(dashboard_df["source"].dropna().astype(str).unique().tolist())
        selected_sources = st.multiselect("Source", options=source_options, default=source_options)
    with filter_col2:
        value_kind_options = sorted(dashboard_df["value_kind"].dropna().astype(str).unique().tolist())
        selected_value_kinds = st.multiselect(
            "Value kind",
            options=value_kind_options,
            default=value_kind_options,
        )

    filtered_df = dashboard_df.copy()
    if selected_sources:
        filtered_df = filtered_df[filtered_df["source"].astype(str).isin(selected_sources)]
    if selected_value_kinds:
        filtered_df = filtered_df[filtered_df["value_kind"].astype(str).isin(selected_value_kinds)]

    st.caption(f"Visualizing {len(filtered_df):,} loaded rows after filters.")
    if filtered_df.empty:
        st.info("No data left after filtering.")
        return

    st.markdown("### Category Distributions")
    dist_col1, dist_col2 = st.columns(2)
    with dist_col1:
        st.caption("Value kind counts")
        value_kind_counts = filtered_df["value_kind"].astype(str).value_counts().sort_index().reset_index()
        value_kind_counts.columns = ["value_kind", "count"]
        st.bar_chart(value_kind_counts.set_index("value_kind"))
    with dist_col2:
        st.caption("Measurement type counts")
        measurement_counts = (
            filtered_df["measurement_type"].astype(str).value_counts().sort_index().reset_index()
        )
        measurement_counts.columns = ["measurement_type", "count"]
        st.bar_chart(measurement_counts.set_index("measurement_type"))

    concentration_df = filtered_df[filtered_df["value_kind"] == "concentration"].copy()
    if not concentration_df.empty:
        st.markdown("### Value Distributions")
        value_col1, value_col2 = st.columns(2)
        with value_col1:
            st.caption("Standard value histogram")
            standard_hist = build_histogram_counts(concentration_df["standard_value_numeric"], bins=30)
            if standard_hist.empty:
                st.info("No standardized numeric values.")
            else:
                st.bar_chart(standard_hist.set_index("bin_start")[["count"]])
        with value_col2:
            st.caption("p-value histogram")
            p_value_hist = build_histogram_counts(concentration_df["p_value_numeric"], bins=30)
            if p_value_hist.empty:
                st.info("No p-values.")
            else:
                st.bar_chart(p_value_hist.set_index("bin_start")[["count"]])

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
        top_compounds = filtered_df["compound"].astype(str).value_counts().head(15).reset_index()
        top_compounds.columns = ["compound", "count"]
        st.bar_chart(top_compounds.set_index("compound"))


def main() -> None:
    st.set_page_config(page_title="Bioactivity Database", layout="wide")
    st.title("Bioactivity Database")

    try:
        endpoints = load_active_endpoint_options()
    except Exception as exc:
        st.error(f"Failed to load active endpoints: {exc}")
        st.stop()

    if not endpoints:
        st.error("No active endpoints found.")
        st.stop()

    labels = [endpoint_label(endpoint) for endpoint in endpoints]
    selected_label = st.selectbox(
        "Endpoint",
        options=labels,
        index=_default_endpoint_index(endpoints),
    )
    selected_endpoint = endpoints[labels.index(selected_label)]

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["Add Compound", "Add Measurement", "Upload CSV", "Browse Results", "Dashboard"]
    )

    with tab1:
        render_compound_tab()
    with tab2:
        render_measurement_tab(selected_endpoint)
    with tab3:
        render_upload_tab(selected_endpoint)
    with tab4:
        render_browse_tab(selected_endpoint)
    with tab5:
        render_dashboard_tab(selected_endpoint)


if __name__ == "__main__":
    main()
