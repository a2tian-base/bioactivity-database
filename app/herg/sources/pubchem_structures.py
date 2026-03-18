#!/usr/bin/env python3
"""
PubChem source adapter for compound structure enrichment.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Sequence

from ..config import DbConfig, HttpConfig
from ..http import get_json
from ..models import CompoundMatchInput, SourceRecordInput, StructureEnrichmentRecord, StructureInput
from ..normalize import build_identifier_inputs, clean_text
from ..read_db import fetch_structure_enrichment_candidates


PUBCHEM_BASE_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"


def _chunked(values: Sequence[int], size: int) -> Iterable[Sequence[int]]:
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


class PubChemStructureAdapter:
    source_name = "pubchem"

    def __init__(
        self,
        http_config: HttpConfig,
        db_config: DbConfig,
        base_url: str = PUBCHEM_BASE_URL,
        limit: int | None = None,
        cid_batch_size: int = 150,
    ) -> None:
        self.http_config = http_config
        self.db_config = db_config
        self.base_url = base_url.rstrip("/")
        self.limit = limit
        self.cid_batch_size = cid_batch_size
        self.enrich_batch_size = max(1, cid_batch_size)

    def iter_raw_rows(self) -> Iterable[dict[str, Any]]:
        rows = fetch_structure_enrichment_candidates("pubchem", limit=self.limit, db_config=self.db_config)
        for row in rows:
            yield {
                **row,
                "external_key": f"compound:{row['compound_id']}|provider:pubchem:structure",
            }

    def enrich_batch(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cids = sorted({int(row["pubchem_cid"]) for row in rows if row.get("pubchem_cid") is not None})
        metadata_map: Dict[int, Dict[str, Any]] = {}

        for batch in _chunked(cids, self.cid_batch_size):
            cid_csv = ",".join(str(cid) for cid in batch)
            url = f"{self.base_url}/compound/cid/{cid_csv}/property/InChI,InChIKey,Title/JSON"
            payload = get_json(url, None, self.http_config, label="PubChem")
            for item in (payload.get("PropertyTable") or {}).get("Properties") or []:
                cid = item.get("CID")
                if cid is None:
                    continue
                metadata_map[int(cid)] = {
                    "CID": cid,
                    "Title": clean_text(item.get("Title")),
                    "InChI": clean_text(item.get("InChI")),
                    "InChIKey": clean_text(item.get("InChIKey")),
                }

        enriched_rows: list[dict[str, Any]] = []
        for row in rows:
            enriched = dict(row)
            cid = row.get("pubchem_cid")
            property_row = metadata_map.get(int(cid)) if cid is not None else None
            enriched["property_row"] = property_row

            if not property_row:
                enriched["harvest_status"] = "unmatched"
                enriched["harvest_reason"] = "No PubChem property row returned."
            elif not clean_text(property_row.get("InChI")) and not clean_text(property_row.get("InChIKey")):
                enriched["harvest_status"] = "unmatched"
                enriched["harvest_reason"] = "PubChem property row had no InChI or InChIKey."
            else:
                enriched["harvest_status"] = "candidate"
                enriched["harvest_reason"] = ""

            enriched_rows.append(enriched)

        return enriched_rows

    def map_row(self, row: dict[str, Any]) -> StructureEnrichmentRecord:
        compound_id = row.get("compound_id")
        cid = row.get("pubchem_cid")
        property_row = row.get("property_row") or {}
        if not compound_id or cid is None or not property_row:
            raise ValueError("Missing PubChem structure enrichment payload.")

        return StructureEnrichmentRecord(
            external_key=f"compound:{compound_id}|provider:pubchem:structure",
            match=CompoundMatchInput(
                standard_inchikey=clean_text(row.get("standard_inchikey")),
                identifiers=build_identifier_inputs(
                    {
                        "pubchem_cid": str(cid),
                        "chembl_id": row.get("chembl_id"),
                        "unii": row.get("unii"),
                    }
                ),
            ),
            structure=StructureInput(
                standard_inchi=clean_text(property_row.get("InChI")),
                standard_inchikey=clean_text(property_row.get("InChIKey")),
            ),
            source_record=SourceRecordInput(
                source_name=self.source_name,
                source_record_key=f"cid:{cid}|properties:structure",
                record_type="structure_enrichment",
                raw_payload={
                    "property_row": property_row,
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
