"""Coverage compiler skill implementation.

Compiles a validated Query Contract into the mandatory coverage cells --
the non-negotiable floor of source x language x region x query-family x
entity-route x time work that must be attempted before a run can claim
provisional saturation (see SPEC.md "Stopping / completion status").

This module does not perform any searching. It only enumerates the
mandatory cells; workers execute them, and a separate evidence-state
layer (not built yet) tracks each cell's status.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

DEFAULT_ENTITY_ROUTES = [
    "patents",
    "literature",
    "trial_registries",
    "company_filings",
    "regulatory_filings",
    "conferences",
]

DEFAULT_REGIONS = [
    {"region": "US", "language": "en"},
    {"region": "EU", "language": "en"},
    {"region": "JP", "language": "ja"},
    {"region": "CN", "language": "zh"},
]

REGION_LANGUAGE_MAP = {
    "US": ["en"],
    "UK": ["en"],
    "EU": ["en"],
    "JP": ["ja"],
    "CN": ["zh"],
    "KR": ["ko"],
    "DE": ["de", "en"],
    "FR": ["fr", "en"],
    "IN": ["en", "hi"],
    "BR": ["pt", "en"],
}


@dataclass
class CoverageCell:
    source: str
    region: str
    language: str
    query_family: str
    entity_route: str
    as_of: str | None
    status: str = "pending"
    note: str | None = None


def _regions_from_contract(scope: dict[str, Any]) -> list[dict[str, str]]:
    geography = scope.get("geography") or []
    if not geography:
        return DEFAULT_REGIONS

    result: list[dict[str, str]] = []
    for g in geography:
        languages = REGION_LANGUAGE_MAP.get(g)
        if languages is None:
            # Unmapped region: fall back to English but flag it explicitly
            # rather than silently guessing, so a human can add the mapping.
            result.append({"region": g, "language": "en", "note": "unmapped_region_defaulted_to_en"})
        else:
            for lang in languages:
                result.append({"region": g, "language": lang})
    return result


def _sources_from_contract(access: dict[str, Any]) -> list[str]:
    sources: list[str] = []
    for key in ("public_sources", "licensed_sources", "subscriber_sources"):
        sources.extend(access.get(key) or [])
    if not sources:
        sources = ["unspecified"]
    return sources


def _query_families_from_contract(scope: dict[str, Any]) -> list[str]:
    families = list(scope.get("target_or_mechanism") or [])
    if not families:
        families = ["unspecified"]
    return families


def compile_coverage(query_contract: dict[str, Any]) -> list[dict[str, Any]]:
    """Compile a validated Query Contract into mandatory coverage cells.

    Args:
        query_contract: The dict produced by the query-contract skill's
            build_query_contract / run.

    Returns:
        A list of coverage cell dicts (source x region x language x
        query_family x entity_route), all starting with status "pending".
        This is a cross-product, not an intelligent plan -- ordering and
        the adaptive frontier happen elsewhere.

    Raises:
        ValueError: if query_contract is missing "scope" or "access", or
            scope.as_of_date is missing.
    """
    if "scope" not in query_contract or "access" not in query_contract:
        raise ValueError(
            "compile_coverage: query_contract missing required 'scope' or 'access' key "
            "-- pass the dict returned by query-contract's build_query_contract/run"
        )

    scope = query_contract["scope"]
    access = query_contract["access"]
    as_of = scope.get("as_of_date")
    if not as_of:
        raise ValueError("compile_coverage: query_contract.scope.as_of_date is required")

    regions = _regions_from_contract(scope)
    sources = _sources_from_contract(access)
    query_families = _query_families_from_contract(scope)

    cells: list[CoverageCell] = []
    for source in sources:
        for region_info in regions:
            for family in query_families:
                for route in DEFAULT_ENTITY_ROUTES:
                    cells.append(
                        CoverageCell(
                            source=source,
                            region=region_info["region"],
                            language=region_info["language"],
                            query_family=family,
                            entity_route=route,
                            as_of=as_of,
                            note=region_info.get("note"),
                        )
                    )

    return [asdict(c) for c in cells]


async def run(*, query_contract: dict[str, Any]) -> list[dict[str, Any]]:
    """Compile a validated Query Contract into mandatory coverage cells.

    See compile_coverage for details.
    """
    return compile_coverage(query_contract=query_contract)
