from __future__ import annotations

import os
from typing import Any

import psycopg
from psycopg.types.json import Json

from .config import DbConfig
from .models import (
    CompoundInput,
    CompoundMatchInput,
    EnrichmentOutcome,
    Ic50Input,
    IdentifierEnrichmentRecord,
    IdentifierInput,
    NameInput,
    SourceRecordInput,
    StructureEnrichmentOutcome,
    StructureEnrichmentRecord,
    StructureInput,
)


class EnrichmentConflictError(Exception):
    pass


class StructureConflictError(EnrichmentConflictError):
    pass


class DatabaseContractError(RuntimeError):
    pass


def _env_value(key: str, default: str) -> str:
    value = os.getenv(key)
    return value if value else default


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _identifier_payload(identifiers: list[IdentifierInput]) -> list[dict[str, object]]:
    return [
        {
            "namespace": identifier.namespace,
            "value": identifier.value,
            "is_primary": identifier.is_primary,
        }
        for identifier in identifiers
    ]


def _name_payload(names: list[NameInput]) -> list[dict[str, object]]:
    return [
        {
            "name": name.name,
            "name_type": name.name_type,
            "is_preferred": name.is_preferred,
        }
        for name in names
    ]


def _structure_payload(structure: StructureInput) -> dict[str, str | None]:
    return {
        "canonical_smiles": _optional_text(structure.canonical_smiles),
        "standard_inchi": _optional_text(structure.standard_inchi),
        "standard_inchikey": _optional_text(structure.standard_inchikey),
    }


def get_conn(
    host: str | None = None,
    port: int | None = None,
    dbname: str | None = None,
    user: str | None = None,
    password: str | None = None,
    db_config: DbConfig | None = None,
) -> psycopg.Connection:
    if db_config is not None:
        host = host or db_config.host
        port = port or db_config.port
        dbname = dbname or db_config.dbname
        user = user or db_config.user
        password = password or db_config.password

    resolved_port = port
    if resolved_port is None:
        resolved_port = int(_env_value("DB_PORT", "5432"))

    return psycopg.connect(
        host=host or _env_value("DB_HOST", "localhost"),
        port=resolved_port,
        dbname=dbname or _env_value("DB_NAME", "herg"),
        user=user or _env_value("DB_USER", "herg_user"),
        password=password or _env_value("DB_PASSWORD", "change_me"),
    )


def upsert_compound(cur: psycopg.Cursor, compound: CompoundInput) -> int:
    cur.execute(
        """
        SELECT register_compound_v2(%s::jsonb, %s::jsonb, %s, %s, %s)
        """,
        (
            Json(_identifier_payload(compound.identifiers)),
            Json(_name_payload(compound.names)),
            _optional_text(compound.canonical_smiles),
            _optional_text(compound.standard_inchi),
            _optional_text(compound.standard_inchikey),
        ),
    )
    row = cur.fetchone()
    return int(row[0])


def upsert_source_record(cur: psycopg.Cursor, source: SourceRecordInput) -> int:
    cur.execute(
        """
        SELECT upsert_source_record(%s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            _optional_text(source.source_name),
            _optional_text(source.source_record_key),
            _optional_text(source.record_type),
            _optional_text(source.source_release),
            _optional_text(source.source_url),
            Json(source.raw_payload or {}),
        ),
    )
    row = cur.fetchone()
    return int(row[0])


def upsert_ic50_result(
    cur: psycopg.Cursor,
    compound_id: int,
    source_record_id: int,
    measurement: Ic50Input,
) -> dict[str, Any]:
    cur.execute(
        """
        SELECT *
        FROM upsert_ic50_result(
            %s::bigint,
            %s::bigint,
            %s::text,
            %s::numeric,
            %s::text,
            %s::char(1)
        )
        """,
        (
            compound_id,
            source_record_id,
            _optional_text(measurement.endpoint),
            measurement.ic50_value,
            measurement.ic50_unit,
            measurement.qualifier,
        ),
    )
    row = cur.fetchone()
    return {
        "result_id": row[0],
        "ic50_um": row[1],
        "pic50": row[2],
        "pic50_qualifier": row[3],
    }


def _missing_regclasses(cur: psycopg.Cursor, names: list[str]) -> list[str]:
    missing: list[str] = []
    for name in names:
        cur.execute("SELECT to_regclass(%s)", (name,))
        if cur.fetchone()[0] is None:
            missing.append(name)
    return missing


def _missing_regprocs(cur: psycopg.Cursor, names: list[str]) -> list[str]:
    missing: list[str] = []
    for name in names:
        cur.execute("SELECT to_regproc(%s)", (name,))
        if cur.fetchone()[0] is None:
            missing.append(name)
    return missing


def ensure_measurement_ingest_schema(cur: psycopg.Cursor) -> None:
    missing_tables = _missing_regclasses(
        cur,
        [
            "compounds",
            "compound_identifiers",
            "compound_names",
            "source_records",
            "ic50_results",
        ],
    )
    missing_functions = _missing_regprocs(
        cur,
        [
            "register_compound_v2",
            "upsert_source_record",
            "upsert_ic50_result",
        ],
    )
    if not missing_tables and not missing_functions:
        return

    details: list[str] = []
    if missing_tables:
        details.append(f"missing tables: {', '.join(missing_tables)}")
    if missing_functions:
        details.append(f"missing functions: {', '.join(missing_functions)}")
    raise DatabaseContractError(
        "Database schema is out of date for measurement ingestion; "
        + "; ".join(details)
        + ". The init SQL in `db/init/001_schema.sql` only runs on a fresh Postgres data volume. "
        + "Rebuild with `docker compose down -v && docker compose up -d --build`."
    )


def ensure_identifier_enrichment_schema(cur: psycopg.Cursor) -> None:
    ensure_measurement_ingest_schema(cur)
    missing_tables = _missing_regclasses(cur, ["compound_identifier_sources"])
    missing_functions = _missing_regprocs(cur, ["resolve_compound_by_keys"])
    if not missing_tables and not missing_functions:
        return

    details: list[str] = []
    if missing_tables:
        details.append(f"missing tables: {', '.join(missing_tables)}")
    if missing_functions:
        details.append(f"missing functions: {', '.join(missing_functions)}")
    raise DatabaseContractError(
        "Database schema is out of date for identifier enrichment; "
        + "; ".join(details)
        + ". Rebuild with `docker compose down -v && docker compose up -d --build`."
    )


def ensure_structure_enrichment_schema(cur: psycopg.Cursor) -> None:
    ensure_measurement_ingest_schema(cur)
    missing_tables = _missing_regclasses(cur, ["compound_structure_assertions"])
    missing_functions = _missing_regprocs(cur, ["resolve_compound_by_keys"])
    if not missing_tables and not missing_functions:
        return

    details: list[str] = []
    if missing_tables:
        details.append(f"missing tables: {', '.join(missing_tables)}")
    if missing_functions:
        details.append(f"missing functions: {', '.join(missing_functions)}")
    raise DatabaseContractError(
        "Database schema is out of date for structure enrichment; "
        + "; ".join(details)
        + ". Rebuild with `docker compose down -v && docker compose up -d --build`."
    )


def resolve_compound_for_enrichment(cur: psycopg.Cursor, match: CompoundMatchInput) -> int | None:
    try:
        cur.execute(
            """
            SELECT resolve_compound_by_keys(%s, %s::jsonb)
            """,
            (
                _optional_text(match.standard_inchikey),
                Json(_identifier_payload(match.identifiers)),
            ),
        )
    except psycopg.Error as exc:
        message = exc.diag.message_primary if exc.diag and exc.diag.message_primary else str(exc)
        raise EnrichmentConflictError(message) from exc

    row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def _attach_identifier_source(
    cur: psycopg.Cursor,
    compound_identifier_id: int,
    source_record_id: int | None,
) -> None:
    if source_record_id is None:
        return
    cur.execute(
        """
        INSERT INTO compound_identifier_sources (compound_identifier_id, source_record_id)
        VALUES (%s, %s)
        ON CONFLICT (compound_identifier_id, source_record_id) DO NOTHING
        """,
        (compound_identifier_id, source_record_id),
    )


def _sync_compound_identifiers(
    cur: psycopg.Cursor,
    compound_id: int,
    identifiers: list[IdentifierInput],
    source_record_id: int | None = None,
    apply_changes: bool = False,
) -> dict[str, int]:
    counts = {"added": 0, "already_present": 0}
    seen: set[tuple[str, str]] = set()

    for identifier in identifiers:
        namespace = _optional_text(identifier.namespace)
        value = _optional_text(identifier.value)
        if namespace is None or value is None:
            continue

        namespace = namespace.lower()
        dedupe_key = (namespace, value.casefold())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        cur.execute(
            """
            SELECT compound_identifier_id, compound_id, is_primary
            FROM compound_identifiers
            WHERE LOWER(BTRIM(namespace)) = %s
              AND normalized_value = normalize_identifier(%s, %s)
            """,
            (namespace, namespace, value),
        )
        existing = cur.fetchone()

        if existing is not None:
            compound_identifier_id, existing_compound_id, existing_is_primary = existing
            if int(existing_compound_id) != compound_id:
                raise EnrichmentConflictError(
                    f"Identifier {namespace}={value} is already assigned to compound_id={existing_compound_id}."
                )

            if identifier.is_primary and not existing_is_primary:
                cur.execute(
                    """
                    SELECT compound_identifier_id
                    FROM compound_identifiers
                    WHERE compound_id = %s
                      AND LOWER(BTRIM(namespace)) = %s
                      AND is_primary
                      AND compound_identifier_id <> %s
                    LIMIT 1
                    """,
                    (compound_id, namespace, compound_identifier_id),
                )
                if cur.fetchone() is not None:
                    raise EnrichmentConflictError(
                        f"Compound {compound_id} already has a different primary identifier in namespace '{namespace}'."
                    )
                if apply_changes:
                    cur.execute(
                        """
                        UPDATE compound_identifiers
                        SET is_primary = TRUE
                        WHERE compound_identifier_id = %s
                        """,
                        (compound_identifier_id,),
                    )

            if apply_changes:
                _attach_identifier_source(cur, int(compound_identifier_id), source_record_id)
            counts["already_present"] += 1
            continue

        if identifier.is_primary:
            cur.execute(
                """
                SELECT compound_identifier_id
                FROM compound_identifiers
                WHERE compound_id = %s
                  AND LOWER(BTRIM(namespace)) = %s
                  AND is_primary
                LIMIT 1
                """,
                (compound_id, namespace),
            )
            if cur.fetchone() is not None:
                raise EnrichmentConflictError(
                    f"Compound {compound_id} already has a primary identifier in namespace '{namespace}'."
                )

        if apply_changes:
            cur.execute(
                """
                INSERT INTO compound_identifiers (
                    compound_id,
                    namespace,
                    identifier_value,
                    is_primary
                )
                VALUES (%s, %s, %s, %s)
                RETURNING compound_identifier_id
                """,
                (compound_id, namespace, value, identifier.is_primary),
            )
            compound_identifier_id = int(cur.fetchone()[0])
            _attach_identifier_source(cur, compound_identifier_id, source_record_id)

        counts["added"] += 1

    return counts


def upsert_compound_identifiers(
    cur: psycopg.Cursor,
    compound_id: int,
    identifiers: list[IdentifierInput],
    source_record_id: int | None = None,
) -> None:
    _sync_compound_identifiers(
        cur,
        compound_id,
        identifiers,
        source_record_id=source_record_id,
        apply_changes=True,
    )


def _sync_compound_names(
    cur: psycopg.Cursor,
    compound_id: int,
    names: list[NameInput],
    apply_changes: bool = False,
) -> dict[str, int]:
    counts = {"added": 0, "already_present": 0}
    seen: set[str] = set()

    for name in names:
        value = _optional_text(name.name)
        if value is None:
            continue

        normalized_name = value.casefold()
        if normalized_name in seen:
            continue
        seen.add(normalized_name)

        name_type = _optional_text(name.name_type) or "alias"
        cur.execute(
            """
            SELECT compound_name_id, is_preferred
            FROM compound_names
            WHERE compound_id = %s
              AND normalized_name = normalize_name(%s)
            """,
            (compound_id, value),
        )
        existing = cur.fetchone()

        if existing is not None:
            compound_name_id, existing_is_preferred = existing
            if name.is_preferred and not existing_is_preferred:
                cur.execute(
                    """
                    SELECT compound_name_id
                    FROM compound_names
                    WHERE compound_id = %s
                      AND is_preferred
                      AND compound_name_id <> %s
                    LIMIT 1
                    """,
                    (compound_id, compound_name_id),
                )
                if cur.fetchone() is not None:
                    raise EnrichmentConflictError(
                        f"Compound {compound_id} already has a different preferred name."
                    )
                if apply_changes:
                    cur.execute(
                        """
                        UPDATE compound_names
                        SET is_preferred = TRUE,
                            name_type = 'preferred'
                        WHERE compound_name_id = %s
                        """,
                        (compound_name_id,),
                    )

            counts["already_present"] += 1
            continue

        if name.is_preferred:
            cur.execute(
                """
                SELECT compound_name_id
                FROM compound_names
                WHERE compound_id = %s
                  AND is_preferred
                LIMIT 1
                """,
                (compound_id,),
            )
            if cur.fetchone() is not None:
                raise EnrichmentConflictError(
                    f"Compound {compound_id} already has a preferred name."
                )

        if apply_changes:
            cur.execute(
                """
                INSERT INTO compound_names (
                    compound_id,
                    name,
                    name_type,
                    is_preferred
                )
                VALUES (%s, %s, %s, %s)
                """,
                (compound_id, value, name_type, name.is_preferred),
            )
        counts["added"] += 1

    return counts


def upsert_compound_names(
    cur: psycopg.Cursor,
    compound_id: int,
    names: list[NameInput],
) -> None:
    _sync_compound_names(cur, compound_id, names, apply_changes=True)


def fetch_compound_structure_state(cur: psycopg.Cursor, compound_id: int) -> dict[str, Any]:
    cur.execute(
        """
        SELECT canonical_smiles, standard_inchi, standard_inchikey
        FROM compounds
        WHERE compound_id = %s
        """,
        (compound_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"Compound {compound_id} does not exist.")
    return {
        "canonical_smiles": _optional_text(row[0]),
        "standard_inchi": _optional_text(row[1]),
        "standard_inchikey": _optional_text(row[2]),
    }


def compute_structure_enrichment_delta(
    current_state: dict[str, Any],
    structure: StructureInput,
) -> dict[str, Any]:
    incoming = _structure_payload(structure)
    current_canonical_smiles = _optional_text(current_state.get("canonical_smiles"))
    current_standard_inchi = _optional_text(current_state.get("standard_inchi"))
    current_standard_inchikey = _optional_text(current_state.get("standard_inchikey"))
    incoming_canonical_smiles = incoming["canonical_smiles"]
    incoming_standard_inchi = incoming["standard_inchi"]
    incoming_standard_inchikey = incoming["standard_inchikey"]

    if (
        current_standard_inchikey
        and incoming_standard_inchikey
        and current_standard_inchikey.upper() != incoming_standard_inchikey.upper()
    ):
        raise StructureConflictError(
            "Incoming standard_inchikey conflicts with the existing compound structure."
        )

    if current_standard_inchi and incoming_standard_inchi and current_standard_inchi != incoming_standard_inchi:
        raise StructureConflictError(
            "Incoming standard_inchi conflicts with the existing compound structure."
        )

    added_fields: list[str] = []
    soft_differences: list[str] = []

    if incoming_standard_inchikey and not current_standard_inchikey:
        added_fields.append("standard_inchikey")

    if incoming_standard_inchi and not current_standard_inchi:
        added_fields.append("standard_inchi")

    if incoming_canonical_smiles:
        if not current_canonical_smiles:
            added_fields.append("canonical_smiles")
        elif current_canonical_smiles != incoming_canonical_smiles:
            soft_differences.append("canonical_smiles")

    return {
        "incoming": incoming,
        "added_fields": tuple(added_fields),
        "soft_differences": tuple(soft_differences),
    }


def upsert_compound_structure_assertion(
    cur: psycopg.Cursor,
    compound_id: int,
    source_record_id: int,
    structure: StructureInput,
) -> bool:
    payload = _structure_payload(structure)
    cur.execute(
        """
        INSERT INTO compound_structure_assertions (
            compound_id,
            source_record_id,
            canonical_smiles,
            standard_inchi,
            standard_inchikey
        )
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (compound_id, source_record_id) DO NOTHING
        RETURNING compound_structure_assertion_id
        """,
        (
            compound_id,
            source_record_id,
            payload["canonical_smiles"],
            payload["standard_inchi"],
            payload["standard_inchikey"],
        ),
    )
    return cur.fetchone() is not None


def preview_structure_enrichment(
    cur: psycopg.Cursor,
    record: StructureEnrichmentRecord,
) -> StructureEnrichmentOutcome:
    compound_id = resolve_compound_for_enrichment(cur, record.match)
    if compound_id is None:
        return StructureEnrichmentOutcome(status="unmatched")

    current_state = fetch_compound_structure_state(cur, compound_id)
    delta = compute_structure_enrichment_delta(current_state, record.structure)
    status = "attached" if delta["added_fields"] else "already_present"
    return StructureEnrichmentOutcome(
        status=status,
        compound_id=compound_id,
        fields_added=delta["added_fields"],
        assertion_stored=False,
    )


def apply_structure_enrichment(
    cur: psycopg.Cursor,
    record: StructureEnrichmentRecord,
) -> StructureEnrichmentOutcome:
    compound_id = resolve_compound_for_enrichment(cur, record.match)
    if compound_id is None:
        return StructureEnrichmentOutcome(status="unmatched")

    source_record_id = upsert_source_record(cur, record.source_record)
    current_state = fetch_compound_structure_state(cur, compound_id)
    delta = compute_structure_enrichment_delta(current_state, record.structure)
    incoming = delta["incoming"]

    if delta["added_fields"]:
        cur.execute(
            """
            UPDATE compounds
            SET
                canonical_smiles = COALESCE(NULLIF(BTRIM(canonical_smiles), ''), %s),
                standard_inchi = COALESCE(NULLIF(BTRIM(standard_inchi), ''), %s),
                standard_inchikey = COALESCE(NULLIF(BTRIM(standard_inchikey), ''), %s)
            WHERE compound_id = %s
            """,
            (
                incoming["canonical_smiles"],
                incoming["standard_inchi"],
                incoming["standard_inchikey"],
                compound_id,
            ),
        )

    assertion_stored = upsert_compound_structure_assertion(
        cur,
        compound_id,
        source_record_id,
        record.structure,
    )
    status = "attached" if delta["added_fields"] else "already_present"
    return StructureEnrichmentOutcome(
        status=status,
        compound_id=compound_id,
        source_record_id=source_record_id,
        fields_added=delta["added_fields"],
        assertion_stored=assertion_stored,
    )


def apply_identifier_enrichment(
    cur: psycopg.Cursor,
    record: IdentifierEnrichmentRecord,
    create_missing_compounds: bool = False,
) -> EnrichmentOutcome:
    source_record_id: int | None = None
    if record.source_record is not None:
        source_record_id = upsert_source_record(cur, record.source_record)

    compound_id = resolve_compound_for_enrichment(cur, record.match)
    created_compound = False

    if compound_id is None:
        if not create_missing_compounds:
            return EnrichmentOutcome(
                status="unmatched",
                source_record_id=source_record_id,
            )

        compound = CompoundInput(
            standard_inchikey=record.match.standard_inchikey,
            identifiers=record.match.identifiers + record.identifiers_to_add,
            names=record.names_to_add,
        )
        compound_id = upsert_compound(cur, compound)
        created_compound = True

    identifier_counts = _sync_compound_identifiers(
        cur,
        compound_id,
        record.identifiers_to_add,
        source_record_id=source_record_id,
        apply_changes=True,
    )
    name_counts = _sync_compound_names(cur, compound_id, record.names_to_add, apply_changes=True)

    if identifier_counts["added"] > 0 or name_counts["added"] > 0 or created_compound:
        status = "attached"
    else:
        status = "already_present"

    return EnrichmentOutcome(
        status=status,
        compound_id=compound_id,
        identifiers_added=identifier_counts["added"],
        names_added=name_counts["added"],
        source_record_id=source_record_id,
        created_compound=created_compound,
    )


def preview_identifier_enrichment(
    cur: psycopg.Cursor,
    record: IdentifierEnrichmentRecord,
    create_missing_compounds: bool = False,
) -> EnrichmentOutcome:
    compound_id = resolve_compound_for_enrichment(cur, record.match)
    created_compound = False

    if compound_id is None:
        if not create_missing_compounds:
            return EnrichmentOutcome(status="unmatched")

        created_compound = True
        identifier_counts = {
            "added": len(
                {
                    (
                        _optional_text(identifier.namespace) or "",
                        (_optional_text(identifier.value) or "").casefold(),
                    )
                    for identifier in (record.match.identifiers + record.identifiers_to_add)
                    if _optional_text(identifier.namespace) and _optional_text(identifier.value)
                }
            ),
            "already_present": 0,
        }
        name_counts = {
            "added": len(
                {
                    (_optional_text(name.name) or "").casefold()
                    for name in record.names_to_add
                    if _optional_text(name.name)
                }
            ),
            "already_present": 0,
        }
        return EnrichmentOutcome(
            status="attached",
            identifiers_added=identifier_counts["added"],
            names_added=name_counts["added"],
            created_compound=created_compound,
        )

    identifier_counts = _sync_compound_identifiers(cur, compound_id, record.identifiers_to_add)
    name_counts = _sync_compound_names(cur, compound_id, record.names_to_add)

    status = "attached"
    if identifier_counts["added"] == 0 and name_counts["added"] == 0:
        status = "already_present"

    return EnrichmentOutcome(
        status=status,
        compound_id=compound_id,
        identifiers_added=identifier_counts["added"],
        names_added=name_counts["added"],
        created_compound=created_compound,
    )
