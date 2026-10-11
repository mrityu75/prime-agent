"""Acquisition worker skill implementation (websearch-based, first worker type).

Executes ONE mandatory coverage cell by building a search query from it and
the originating query contract, then calling the existing websearch skill.
Returns raw, structured findings tagged with the cell they came from -- this
worker does not judge relevance or admit anything; per SPEC.md's Agent
execution boundary, workers commit structured deltas, never authoritative
prose lists or admission decisions.

This is the first of several planned worker types (patents, trial registries,
company filings, etc. -- see SPEC.md Acquisition layer). It intentionally
covers only general web/literature search via Serper, reusing the existing
websearch skill rather than duplicating its HTTP logic.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

WEBSEARCH_FAILURE_SIGNALS = [
    "Web search is not set up yet",
    "Error searching for '",
]


def _is_websearch_failure(text: str) -> bool:
    return any(signal in text for signal in WEBSEARCH_FAILURE_SIGNALS)


ROUTE_TERMS = {
    "patents": "compounds patent application WO",
    "literature": "discovery medicinal chemistry journal article",
    "trial_registries": "clinical trial phase NCT",
    "company_filings": "pipeline development candidate company press release",
    "regulatory_filings": "IND clearance clinical trial application",
    "conferences": "first-in-human abstract poster AACR ASCO ESMO",
}

ROUTE_REGION_HINTS = {
    "patents": {"US": "US patent", "EU": "EP patent", "JP": "JP patent", "CN": "CN patent"},
    "trial_registries": {"US": "ClinicalTrials.gov", "EU": "EU CTR EudraCT", "JP": "jRCT", "CN": "CTR China"},
    "regulatory_filings": {"US": "FDA", "EU": "EMA", "JP": "PMDA", "CN": "NMPA"},
}


def _build_search_query(cell: dict[str, Any], query_contract: dict[str, Any]) -> str:
    """Build a plain-text search query from a coverage cell and its query contract.

    The query is shaped per entity route: the query family (usually the
    target/mechanism) and modality, plus terms that match how that route's
    sources are written (ROUTE_TERMS) and, where it helps, a source name for
    the region (ROUTE_REGION_HINTS, e.g. jRCT for JP trial registries).
    Other regions fall back to the region code. Unknown routes fall back to
    the route name. The terms are generic per route and never derived from
    gold data. Queries stay in English; cells with a non-English language are
    not translated yet.
    """
    family = cell.get("query_family", "")
    route = cell.get("entity_route", "")
    region = cell.get("region", "")
    modality = (query_contract.get("scope") or {}).get("modality") or []
    modality_str = " ".join(modality)

    route_terms = ROUTE_TERMS.get(route, route.replace("_", " "))
    region_hint = ROUTE_REGION_HINTS.get(route, {}).get(region, region)
    parts = [p for p in [family, modality_str, route_terms, region_hint] if p]
    return " ".join(parts)


async def run(
    *,
    cell: dict[str, Any],
    query_contract: dict[str, Any],
    websearch_fn: Callable[[str], Awaitable[str]] | None = None,
) -> dict[str, Any]:
    """Execute one mandatory coverage cell via web search and return raw findings.

    Args:
        cell: One coverage cell dict, as produced by the coverage-compiler skill.
        query_contract: The originating query contract, as produced by the
            query-contract skill. Used for extra search context (e.g. modality).
        websearch_fn: The websearch skill's callable. Injected as a parameter
            (rather than imported directly) so this module stays testable in
            isolation, outside the Prime Agent kernel where the real
            `websearch` name is only available as a kernel global. In the
            kernel, callers should pass the global `websearch` directly:
                await acquisition_worker(cell=..., query_contract=..., websearch_fn=websearch)

    Returns:
        A dict: {"cell": <the input cell, unchanged>, "query": <search string
        used>, "findings_text": <raw formatted text from websearch>,
        "status": "completed" or "failed", "error": <str or None>}.
        websearch reports failures as returned text, not exceptions, so text
        matching WEBSEARCH_FAILURE_SIGNALS is returned as "failed" with the
        text in "error" instead of being trusted as findings.
        This worker does not parse findings_text into structured records --
        that is a later, separate step (evidence-state layer), kept out of
        this worker so it stays a thin, single-purpose acquisition step.
    """
    if websearch_fn is None:
        return {
            "cell": cell,
            "query": None,
            "findings_text": None,
            "status": "failed",
            "error": "acquisition_worker: no websearch_fn provided. In the kernel, "
            "call with websearch_fn=websearch.",
        }

    query = _build_search_query(cell, query_contract)
    if not query.strip():
        return {
            "cell": cell,
            "query": query,
            "findings_text": None,
            "status": "failed",
            "error": "acquisition_worker: built an empty search query from this cell",
        }

    try:
        findings_text = await websearch_fn(query)
    except Exception as e:  # noqa: BLE001 -- report any search failure, never crash the run
        return {
            "cell": cell,
            "query": query,
            "findings_text": None,
            "status": "failed",
            "error": f"acquisition_worker: websearch call failed: {e}",
        }

    if _is_websearch_failure(findings_text):
        return {
            "cell": cell,
            "query": query,
            "findings_text": None,
            "status": "failed",
            "error": findings_text,
        }

    return {
        "cell": cell,
        "query": query,
        "findings_text": findings_text,
        "status": "completed",
        "error": None,
    }
