"""Query contract skill implementation.

Validates and structures a natural-language drug-sourcing query into the
typed Query Contract defined in the project's SPEC.md (Scope, Semantics,
Evidence, Access). Extraction of field values from the raw query is done
by the calling agent's own reasoning, not by this module -- this module
only validates, applies defaults, and serializes.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, asdict
from datetime import date
from typing import Any


VALID_COUNTING_UNITS = {"asset", "program", "patent_family"}
VALID_AMBIGUITY_POLICIES = {"include_and_flag", "exclude"}


@dataclass
class Scope:
    target_or_mechanism: list[str] = field(default_factory=list)
    modality: list[str] = field(default_factory=list)
    indication: list[str] = field(default_factory=list)
    stage: list[str] = field(default_factory=list)
    geography: list[str] = field(default_factory=list)
    sponsor_constraints: str | None = None
    exclusions: list[str] = field(default_factory=list)
    as_of_date: str | None = None


@dataclass
class Semantics:
    counting_unit: str = "asset"
    inference_rules: str | None = None
    ambiguity_policy: str = "include_and_flag"
    definitions: dict[str, str] = field(default_factory=dict)


@dataclass
class EvidenceRequirements:
    required_output_fields: list[str] = field(default_factory=list)
    acceptable_source_classes: list[str] = field(default_factory=list)
    full_document_required: bool = True
    source_cutoff: str | None = None


@dataclass
class AccessDeclaration:
    public_sources: list[str] = field(default_factory=list)
    licensed_sources: list[str] = field(default_factory=list)
    subscriber_sources: list[str] = field(default_factory=list)
    blocked_sources: list[str] = field(default_factory=list)


def _parse_date(value: str | None, field_name: str, errors: list[str]) -> None:
    if value is None:
        return
    try:
        date.fromisoformat(value)
    except ValueError:
        errors.append(f"{field_name}: '{value}' is not a valid YYYY-MM-DD date")


def _check_fields(section: str, data: dict[str, Any], cls: type, errors: list[str]) -> None:
    valid = [f.name for f in fields(cls)]
    for key in data:
        if key not in valid:
            errors.append(
                f"{section}: unexpected field '{key}', valid fields are: {', '.join(valid)}"
            )


def build_query_contract(
    *,
    raw_query: str,
    scope: dict[str, Any],
    semantics: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    access: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and structure a query into the typed Query Contract.

    Args:
        raw_query: The original natural-language request, verbatim.
        scope: Scope fields -- see SKILL.md for the full field list.
        semantics: Semantics fields. Optional; defaults applied if omitted.
        evidence: Evidence requirement fields. Optional.
        access: Access declaration fields. Optional.

    Returns:
        A JSON-serializable dict with the validated, structured contract.

    Raises:
        ValueError: if raw_query is empty, any section has an unexpected field,
            scope.as_of_date is missing/invalid,
            counting_unit or ambiguity_policy is not a recognized value, or any
            date field is not valid YYYY-MM-DD. The error lists every problem
            found, not just the first.
    """
    errors: list[str] = []

    if not raw_query or not raw_query.strip():
        errors.append("raw_query: must not be empty")

    scope = dict(scope or {})
    semantics = dict(semantics or {})
    evidence = dict(evidence or {})
    access = dict(access or {})

    _check_fields("scope", scope, Scope, errors)
    _check_fields("semantics", semantics, Semantics, errors)
    _check_fields("evidence", evidence, EvidenceRequirements, errors)
    _check_fields("access", access, AccessDeclaration, errors)

    if not scope.get("as_of_date"):
        errors.append("scope.as_of_date: required, got none")
    _parse_date(scope.get("as_of_date"), "scope.as_of_date", errors)

    counting_unit = semantics.get("counting_unit", "asset")
    if counting_unit not in VALID_COUNTING_UNITS:
        errors.append(
            f"semantics.counting_unit: '{counting_unit}' not in {sorted(VALID_COUNTING_UNITS)}"
        )

    ambiguity_policy = semantics.get("ambiguity_policy", "include_and_flag")
    if ambiguity_policy not in VALID_AMBIGUITY_POLICIES:
        errors.append(
            f"semantics.ambiguity_policy: '{ambiguity_policy}' not in {sorted(VALID_AMBIGUITY_POLICIES)}"
        )

    _parse_date(evidence.get("source_cutoff"), "evidence.source_cutoff", errors)

    if not evidence.get("required_output_fields"):
        errors.append("evidence.required_output_fields: required, must be a non-empty list")

    if errors:
        raise ValueError("Query contract validation failed:\n  - " + "\n  - ".join(errors))

    contract = {
        "raw_query": raw_query.strip(),
        "scope": asdict(Scope(**scope)),
        "semantics": asdict(Semantics(**semantics)),
        "evidence": asdict(EvidenceRequirements(**evidence)),
        "access": asdict(AccessDeclaration(**access)),
    }
    return contract


async def run(
    *,
    raw_query: str,
    scope: dict[str, Any],
    semantics: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    access: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and structure a query into the typed Query Contract.

    Args:
        raw_query: The original natural-language request, verbatim.
        scope: Scope fields -- see SKILL.md for the full field list.
        semantics: Semantics fields. Optional; defaults applied if omitted.
        evidence: Evidence requirement fields. Optional.
        access: Access declaration fields. Optional.

    Returns:
        A JSON-serializable dict with the validated, structured contract.

    Raises:
        ValueError: if validation fails. The error lists every problem found.
    """
    return build_query_contract(
        raw_query=raw_query,
        scope=scope,
        semantics=semantics,
        evidence=evidence,
        access=access,
    )
