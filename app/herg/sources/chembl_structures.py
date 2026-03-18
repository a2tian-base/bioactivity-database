#!/usr/bin/env python3
"""
ChEMBL source adapter for compound structure enrichment.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Sequence

from ..config import DbConfig, HttpConfig
from ..http import get_json
from ..models import CompoundMatchInput, SourceRecordInput, StructureEnrichmentRecord, StructureInput
from ..normalize import build_identifier_inputs, clean_text
from ..read_db import fetch_structure_enrichment_candidates


CHEMBL_BASE_URL = "https://www.ebi.ac.uk/chembl/api/data"
MOLECULE_ONLY_FIELDS = "molecule_chembl_id,pref_name,molecule_structures"


def _chunked(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


class ChemblStructureAdapter:
    source_name = "chembl"

    def __init__(
        self,
        http_config: HttpConfig,
        db_config: DbConfig,
        base_url: str = CHEMBL_BASE_URL,
        limit: int | None = None,
        molecule_batch_size: int = 150,
    ) -> None:
        self.http_config = http_config
        self.db_config = db_config
        self.base_url = base_url.rstrip("/")
        self.limit = limit
        self.molecule_batch_size = molecule_batch_size
        self.enrich_batch_size = max(1, molecule_batch_size)
        self.release = self._fetch_release()

    def _fetch_release(self) -> str:
        status_url = f"{self.base_url}/status.json"
        status = get_json(status_url, {}, self.http_config, label="ChEMBL")
        return str(status.get("chembl_db_version") or "unknown")

    def iter_raw_rows(self) -> Iterable[dict[str, Any]]:
        rows = fetch_structure_enrichment_candidates("chembl", limit=self.limit, db_config=self.db_config)
        for row in rows:
            yield {
                **row,
                "external_key": f"compound:{row['compound_id']}|provider:chembl:structure",
            }

    def enrich_batch(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        chembl_ids = sorted({clean_text(row.get("chembl_id")) for row in rows if clean_text(row.get("chembl_id"))})
        molecule_map: Dict[str, Dict[str, Any]] = {}
        molecules_url = f"{self.base_url}/molecule.json"

        for batch in _chunked(chembl_ids, self.molecule_batch_size):
            payload = get_json(
                molecules_url,
                {
                    "molecule_chembl_id__in": ",".join(batch),
                    "only": MOLECULE_ONLY_FIELDS,
                    "limit": len(batch),
                },
                self.http_config,
                label="ChEMBL",
            )
            for molecule in payload.get("molecules") or []:
                chembl_id = clean_text(molecule.get("molecule_chembl_id"))
                if chembl_id:
                    molecule_map[chembl_id] = molecule

        enriched_rows: list[dict[str, Any]] = []
        for row in rows:
            enriched = dict(row)
            chembl_id = clean_text(row.get("chembl_id"))
            molecule = molecule_map.get(chembl_id)
            enriched["molecule"] = molecule
            enriched["source_release"] = self.release

            if not molecule:
                enriched["harvest_status"] = "unmatched"
                enriched["harvest_reason"] = "No ChEMBL molecule payload returned."
            else:
                structures = molecule.get("molecule_structures") or {}
                if not any(
                    clean_text(structures.get(field))
                    for field in ("canonical_smiles", "standard_inchi", "standard_inchi_key")
                ):
                    enriched["harvest_status"] = "unmatched"
                    enriched["harvest_reason"] = "ChEMBL molecule payload had no structure fields."
                else:
                    enriched["harvest_status"] = "candidate"
                    enriched["harvest_reason"] = ""

            enriched_rows.append(enriched)

        return enriched_rows

    def map_row(self, row: dict[str, Any]) -> StructureEnrichmentRecord:
        compound_id = row.get("compound_id")
        chembl_id = clean_text(row.get("chembl_id"))
        molecule = row.get("molecule") or {}
        if not compound_id or not chembl_id or not molecule:
            raise ValueError("Missing ChEMBL structure enrichment payload.")

        structures = molecule.get("molecule_structures") or {}
        structure = StructureInput(
            canonical_smiles=clean_text(structures.get("canonical_smiles")),
            standard_inchi=clean_text(structures.get("standard_inchi")),
            standard_inchikey=clean_text(structures.get("standard_inchi_key")),
        )

        return StructureEnrichmentRecord(
            external_key=f"compound:{compound_id}|provider:chembl:structure",
            match=CompoundMatchInput(
                standard_inchikey=clean_text(row.get("standard_inchikey")),
                identifiers=build_identifier_inputs(
                    {
                        "chembl_id": chembl_id,
                        "pubchem_cid": row.get("pubchem_cid"),
                        "unii": row.get("unii"),
                    }
                ),
            ),
            structure=structure,
            source_record=SourceRecordInput(
                source_name=self.source_name,
                source_record_key=f"molecule:{chembl_id}",
                record_type="structure_enrichment",
                source_release=clean_text(row.get("source_release")),
                raw_payload={
                    "molecule": molecule,
                    "seed_row": {
                        "compound_id": compound_id,
                        "standard_inchikey": row.get("standard_inchikey"),
                        "chembl_id": row.get("chembl_id"),
                        "pubchem_cid": row.get("pubchem_cid"),
                        "unii": row.get("unii"),
                        "preferred_name": row.get("preferred_name"),
                        "canonical_smiles": row.get("canonical_smiles"),
                        "standard_inchi": row.get("standard_inchi"),
                    },
                },
            ),
        )
