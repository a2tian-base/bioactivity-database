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


@dataclass(frozen=True)
class StagedRecord:
    external_key: str
    compound: CompoundInput
    source_record: SourceRecordInput
    measurement: Ic50Input


@dataclass(frozen=True)
class CompoundMatchInput:
    standard_inchikey: str = ""
    identifiers: list[IdentifierInput] = field(default_factory=list)


@dataclass(frozen=True)
class IdentifierEnrichmentRecord:
    external_key: str
    match: CompoundMatchInput
    identifiers_to_add: list[IdentifierInput]
    names_to_add: list[NameInput] = field(default_factory=list)
    source_record: SourceRecordInput | None = None


@dataclass(frozen=True)
class EnrichmentOutcome:
    status: str
    compound_id: int | None = None
    identifiers_added: int = 0
    names_added: int = 0
    source_record_id: int | None = None
    created_compound: bool = False


@dataclass(frozen=True)
class StructureInput:
    canonical_smiles: str = ""
    standard_inchi: str = ""
    standard_inchikey: str = ""
    connectivity_smiles: str = ""


@dataclass(frozen=True)
class StructureEnrichmentRecord:
    external_key: str
    match: CompoundMatchInput
    structure: StructureInput
    source_record: SourceRecordInput


@dataclass(frozen=True)
class StructureEnrichmentOutcome:
    status: str
    compound_id: int | None = None
    source_record_id: int | None = None
    fields_added: tuple[str, ...] = ()
    assertion_stored: bool = False
