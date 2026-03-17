from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class IdentifierInput:
    namespace: str
    value: str
    is_primary: bool = False


@dataclass(frozen=True)
class NameInput:
    name: str
    name_type: str = "alias"
    is_preferred: bool = False


@dataclass(frozen=True)
class CompoundInput:
    canonical_smiles: str = ""
    standard_inchi: str = ""
    standard_inchikey: str = ""
    identifiers: list[IdentifierInput] = field(default_factory=list)
    names: list[NameInput] = field(default_factory=list)


@dataclass(frozen=True)
class SourceRecordInput:
    source_name: str
    source_record_key: str
    record_type: str
    source_release: str = ""
    source_url: str = ""
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Ic50Input:
    ic50_value: float
    ic50_unit: str
    qualifier: str
    endpoint: str = "IC50"
