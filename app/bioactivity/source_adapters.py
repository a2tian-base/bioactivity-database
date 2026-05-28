from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from herg.config import HttpConfig
from herg.normalize import clean_text
from herg.pipeline import SourceAdapter

from .endpoints import EndpointConfig


SUPPORTED_SOURCES = frozenset({"chembl", "pubchem"})


def normalize_source_name(source_name: str) -> str:
    normalized = clean_text(source_name).lower()
    if not normalized:
        raise ValueError("source_name is required.")
    if normalized not in SUPPORTED_SOURCES:
        allowed = ", ".join(sorted(SUPPORTED_SOURCES))
        raise ValueError(f"Unsupported source '{normalized}'. Supported sources: {allowed}.")
    return normalized


def build_source_adapter(
    *,
    endpoint: EndpointConfig,
    source_name: str,
    source_config: Mapping[str, Any],
    http_config: HttpConfig,
    chembl_base_url: str | None = None,
    pubchem_base_url: str | None = None,
    activity_page_size: int | None = None,
    molecule_batch_size: int | None = None,
    cid_batch_size: int | None = None,
) -> SourceAdapter:
    normalized_source_name = normalize_source_name(source_name)
    clean_config = dict(source_config)

    if normalized_source_name == "chembl":
        from herg.sources.chembl import CHEMBL_BASE_URL, ChemblAdapter

        return ChemblAdapter.from_source_config(
            endpoint,
            clean_config,
            http_config=http_config,
            base_url=chembl_base_url or CHEMBL_BASE_URL,
            activity_page_size=activity_page_size,
            molecule_batch_size=molecule_batch_size,
        )

    if normalized_source_name == "pubchem":
        from herg.sources.pubchem import PUBCHEM_BASE_URL, PubChemAdapter

        return PubChemAdapter.from_source_config(
            endpoint,
            clean_config,
            http_config=http_config,
            base_url=pubchem_base_url or PUBCHEM_BASE_URL,
            cid_batch_size=cid_batch_size,
        )

    raise AssertionError(f"Unhandled source '{normalized_source_name}'.")
