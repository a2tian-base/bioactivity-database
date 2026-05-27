from __future__ import annotations

import uuid
from typing import Any

import pandas as pd
import streamlit as st
from psycopg.errors import UniqueViolation

from bioactivity.db import upsert_bioactivity_result
from bioactivity.endpoints import EndpointConfig, list_active_endpoints, load_endpoint
from bioactivity.models import MeasurementInput, measurement_from_ic50
from bioactivity.preview import PreviewExample, PreviewResult, preview_endpoint_source
from bioactivity.results import (
    count_bioactivity_results,
    fetch_bioactivity_results,
    format_bioactivity_result_row,
    manual_entry_schema,
)
from bioactivity.ui_ingestion import UiIngestionRequest, UiIngestionResult, run_ui_ingestion
from herg.config import HttpConfig
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


def histogram_chart_data(histogram_counts: pd.DataFrame) -> pd.DataFrame:
    chart_data = histogram_counts.copy()
    chart_data["bin_start_label"] = chart_data["bin_start"].map(lambda value: f"{value:.2f}")
    return chart_data.set_index("bin_start_label")[["count"]].rename_axis("bin_start")


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


def _preview_examples_dataframe(examples: list[PreviewExample]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for example in examples:
        rows.append(
            {
                "external_key": example.external_key,
                "source_record_key": example.source_record_key,
                "measurement": example.measurement,
                "raw_summary": example.raw_summary,
                "reason": example.reason,
            }
        )
    return pd.DataFrame(rows)


def preview_result_display_data(result: PreviewResult) -> dict[str, Any]:
    return {
        "summary": {
            "raw_rows_examined": result.raw_rows_examined,
            "accepted": result.accepted_count,
            "skipped": result.skipped_count,
            "errors": result.error_count,
        },
        "query_config": result.query_config,
        "accepted": _preview_examples_dataframe(result.accepted_examples),
        "skipped": _preview_examples_dataframe(result.skipped_examples),
        "errors": _preview_examples_dataframe(result.error_examples),
        "warnings": result.warnings,
    }


def ingestion_result_summary(result: UiIngestionResult) -> dict[str, Any]:
    return {
        "endpoint_key": result.endpoint_key,
        "source_name": result.source_name,
        "dry_run": result.dry_run,
        "processed": result.processed,
        "stored": result.stored,
        "updated": result.updated,
        "skipped_invalid": result.skipped_invalid,
        "failed": result.failed,
        "warnings": result.warnings,
        "duration_seconds": result.duration_seconds,
        "ingestion_run_id": result.ingestion_run_id,
    }


def render_preview_result(result: PreviewResult) -> None:
    display = preview_result_display_data(result)
    summary = display["summary"]
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Examined", f"{summary['raw_rows_examined']:,}")
    col2.metric("Accepted", f"{summary['accepted']:,}")
    col3.metric("Skipped", f"{summary['skipped']:,}")
    col4.metric("Errors", f"{summary['errors']:,}")

    st.markdown("### Query Config")
    st.json(display["query_config"], expanded=False)

    st.markdown("### Accepted Examples")
    if display["accepted"].empty:
        st.info("No accepted examples.")
    else:
        st.dataframe(display["accepted"], use_container_width=True, hide_index=True)

    st.markdown("### Skipped Examples")
    if display["skipped"].empty:
        st.info("No skipped examples.")
    else:
        st.dataframe(display["skipped"], use_container_width=True, hide_index=True)

    if not display["errors"].empty:
        st.markdown("### Error Examples")
        st.dataframe(display["errors"], use_container_width=True, hide_index=True)

    if display["warnings"]:
        st.warning("\n".join(str(warning) for warning in display["warnings"]))


def render_ingestion_result(result: UiIngestionResult) -> None:
    summary = ingestion_result_summary(result)
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Processed", f"{summary['processed']:,}")
    col2.metric("Stored", f"{summary['stored']:,}")
    col3.metric("Skipped", f"{summary['skipped_invalid']:,}")
    col4.metric("Failed", f"{summary['failed']:,}")

    detail_col1, detail_col2, detail_col3, detail_col4 = st.columns(4)
    detail_col1.metric("Updated", f"{summary['updated']:,}")
    detail_col2.metric("Warnings", f"{summary['warnings']:,}")
    detail_col3.metric("Seconds", f"{summary['duration_seconds']:.2f}")
    detail_col4.metric("Run ID", summary["ingestion_run_id"] or "-")

    if result.dry_run:
        st.info("Dry-run ingestion completed without writing records.")
    elif result.failed > 0:
        st.warning("Ingestion completed with failed rows.")
    else:
        st.success("Ingestion completed and records were written.")
    st.caption("Refresh Browse Results to see newly ingested records.")


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


def _measurement_from_concentration_form(
    *,
    source_record_key: str,
    measurement_type: str,
    value: float,
    unit: str,
    relation: str,
    canonical_unit: str | None,
) -> MeasurementInput:
    same_unit_as_canonical = bool(canonical_unit and unit == canonical_unit)
    return MeasurementInput(
        result_key=source_record_key,
        measurement_type=measurement_type,
        value_kind="concentration",
        original_value=value,
        original_unit=unit,
        original_relation=relation,
        standard_value=value if same_unit_as_canonical else None,
        standard_unit=canonical_unit if same_unit_as_canonical else None,
        standard_relation=relation if same_unit_as_canonical else None,
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


def render_ingest_tab(selected_endpoint: EndpointConfig) -> None:
    st.subheader("Ingest Source Rows")
    st.caption(f"Endpoint: {endpoint_label(selected_endpoint)}")

    source_names = sorted(str(source_name) for source_name in selected_endpoint.source_configs)
    if not source_names:
        st.info("No source configs are available for this endpoint.")
        return

    key_prefix = f"ingest_{selected_endpoint.endpoint_key}"
    selected_source = st.selectbox("Source", options=source_names, key=f"{key_prefix}_source")

    config_col1, config_col2 = st.columns(2)
    with config_col1:
        request_timeout_seconds = st.number_input(
            "Request timeout seconds",
            min_value=1,
            max_value=300,
            value=45,
            step=1,
            key=f"{key_prefix}_timeout",
        )
    with config_col2:
        http_retries = st.number_input(
            "HTTP retries",
            min_value=0,
            max_value=10,
            value=4,
            step=1,
            key=f"{key_prefix}_retries",
        )

    st.markdown("### Preview Source Rows")
    preview_limit = st.number_input(
        "Preview limit",
        min_value=1,
        max_value=200,
        value=20,
        step=1,
        key=f"{key_prefix}_preview_limit",
    )
    if st.button("Preview source rows", key=f"{key_prefix}_preview_button"):
        try:
            with st.spinner("Previewing source rows..."):
                with get_conn() as conn:
                    preview_result = preview_endpoint_source(
                        conn,
                        endpoint_key=selected_endpoint.endpoint_key,
                        source_name=selected_source,
                        limit=int(preview_limit),
                        http_config=HttpConfig(
                            request_timeout_seconds=int(request_timeout_seconds),
                            http_retries=int(http_retries),
                        ),
                    )
            render_preview_result(preview_result)
        except Exception as exc:
            st.error(f"Preview failed: {exc}")

    st.markdown("### Run Ingestion")
    dry_run = st.checkbox("Dry run", value=True, key=f"{key_prefix}_dry_run")
    limit_records = st.checkbox("Limit records", value=True, key=f"{key_prefix}_limit_records")
    ingestion_col1, ingestion_col2 = st.columns(2)
    with ingestion_col1:
        max_records = st.number_input(
            "Max records",
            min_value=1,
            max_value=250000,
            value=100,
            step=10,
            disabled=not limit_records,
            key=f"{key_prefix}_max_records",
        )
    with ingestion_col2:
        commit_every = st.number_input(
            "Commit every",
            min_value=1,
            max_value=10000,
            value=500,
            step=50,
            key=f"{key_prefix}_commit_every",
        )
    fail_fast = st.checkbox("Fail fast", value=False, key=f"{key_prefix}_fail_fast")

    confirmed_write = True
    if not dry_run:
        confirmed_write = st.checkbox(
            "I understand this will write records to the database.",
            value=False,
            key=f"{key_prefix}_confirm_write",
        )
        if not confirmed_write:
            st.warning("Write ingestion requires confirmation.")

    if st.button(
        "Run ingestion",
        disabled=not dry_run and not confirmed_write,
        key=f"{key_prefix}_run_button",
    ):
        request = UiIngestionRequest(
            endpoint_key=selected_endpoint.endpoint_key,
            source_name=selected_source,
            dry_run=bool(dry_run),
            max_records=int(max_records) if limit_records else None,
            commit_every=int(commit_every),
            fail_fast=bool(fail_fast),
            request_timeout_seconds=int(request_timeout_seconds),
            http_retries=int(http_retries),
        )
        try:
            with st.spinner("Running ingestion..."):
                result = run_ui_ingestion(request)
            render_ingestion_result(result)
        except Exception as exc:
            st.error(f"Ingestion failed: {exc}")


def render_browse_tab(selected_endpoint: EndpointConfig) -> None:
    st.subheader("Browse Results")
    st.caption(f"Endpoint: {endpoint_label(selected_endpoint)}")
    limit = st.slider("Rows to preview", min_value=10, max_value=1000, value=100, step=10)

    try:
        with get_conn() as conn:
            total_results = count_bioactivity_results(conn, selected_endpoint.endpoint_id)
            rows = fetch_bioactivity_results(conn, endpoint_id=selected_endpoint.endpoint_id, limit=limit)
        results_df = _format_results_dataframe(rows)
        if total_results == 0 or results_df.empty:
            st.info("No normalized bioactivity results found for this endpoint.")
        else:
            st.caption(
                f"Previewing {len(results_df):,} of {total_results:,} bioactivity_results rows. "
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
                    with get_conn() as conn:
                        full_rows = fetch_bioactivity_results(
                            conn,
                            endpoint_id=selected_endpoint.endpoint_id,
                            limit=None,
                        )
                    full_results_df = _format_results_dataframe(full_rows)
                st.caption(f"Full export contains {len(full_results_df):,} rows.")
                st.download_button(
                    label="Download full results CSV",
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
        with get_conn() as conn:
            total_results = count_bioactivity_results(conn, selected_endpoint.endpoint_id)
            rows = fetch_bioactivity_results(conn, endpoint_id=selected_endpoint.endpoint_id, limit=max_rows)
        dashboard_df = _format_results_dataframe(rows)
    except Exception as exc:
        st.error(f"Failed to load dashboard data: {exc}")
        total_results = 0
        dashboard_df = pd.DataFrame()

    if dashboard_df.empty:
        st.info("No normalized bioactivity data available for this endpoint yet.")
        return

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
                st.bar_chart(histogram_chart_data(standard_hist))
        with value_col2:
            st.caption("p-value histogram")
            p_value_hist = build_histogram_counts(concentration_df["p_value_numeric"], bins=30)
            if p_value_hist.empty:
                st.info("No p-values.")
            else:
                st.bar_chart(histogram_chart_data(p_value_hist))

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

    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
        ["Add Compound", "Add Measurement", "Upload CSV", "Ingest", "Browse Results", "Dashboard"]
    )

    with tab1:
        render_compound_tab()
    with tab2:
        render_measurement_tab(selected_endpoint)
    with tab3:
        render_upload_tab(selected_endpoint)
    with tab4:
        render_ingest_tab(selected_endpoint)
    with tab5:
        render_browse_tab(selected_endpoint)
    with tab6:
        render_dashboard_tab(selected_endpoint)


if __name__ == "__main__":
    main()
