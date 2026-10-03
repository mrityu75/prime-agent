"""Evidence-store skill implementation.

Persists pipeline output (runs, coverage cells, and acquisition-worker
raw mentions) to Supabase via its REST API (PostgREST), using the
SUPABASE_URL and SUPABASE_SECRET_KEY environment variables.

This module does not create tables -- the schema (runs, coverage_cells,
raw_mentions) must already exist in the Supabase project. See SPEC.md
for the schema definition.

This module does no judgment: it only stores what it's given. Admission
and claim extraction are separate, later layers.
"""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable

import httpx

Poster = Callable[[str, dict[str, Any]], Awaitable[list[dict[str, Any]]]]


def _supabase_config() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SECRET_KEY", "")
    return url, key


async def _default_post(table: str, row: dict[str, Any]) -> list[dict[str, Any]]:
    """Post one row to a Supabase table via PostgREST and return the inserted row(s)."""
    url, key = _supabase_config()
    if not url or not key:
        raise RuntimeError(
            "evidence-store: SUPABASE_URL or SUPABASE_SECRET_KEY not set in the environment"
        )
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{url}/rest/v1/{table}",
            json=row,
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
        )
        resp.raise_for_status()
        return resp.json()


async def create_run(
    *,
    raw_query: str,
    query_contract: dict[str, Any],
    post_fn: Poster | None = None,
) -> dict[str, Any]:
    """Create one row in the runs table.

    Args:
        raw_query: The original natural-language request.
        query_contract: The structured query contract dict.
        post_fn: Injectable poster for testing; defaults to a real Supabase
            REST call.

    Returns:
        The inserted run row, including its generated id.

    Raises:
        RuntimeError: if the insert fails or returns no row.
    """
    poster = post_fn or _default_post
    rows = await poster("runs", {"raw_query": raw_query, "query_contract": query_contract})
    if not rows:
        raise RuntimeError("evidence-store: insert into runs returned no row")
    return rows[0]


async def store_coverage_cells(
    *,
    run_id: str,
    cells: list[dict[str, Any]],
    post_fn: Poster | None = None,
) -> list[dict[str, Any]]:
    """Insert coverage cells for a run, one row per cell.

    Args:
        run_id: The id of the run these cells belong to (from create_run).
        cells: Coverage cell dicts as produced by the coverage-compiler skill.
        post_fn: Injectable poster for testing.

    Returns:
        The inserted coverage_cells rows, including generated ids, in the
        same order as the input cells.

    Raises:
        RuntimeError: if any insert fails or returns no row.
    """
    poster = post_fn or _default_post
    inserted: list[dict[str, Any]] = []
    for cell in cells:
        row = {
            "run_id": run_id,
            "source": cell.get("source"),
            "region": cell.get("region"),
            "language": cell.get("language"),
            "query_family": cell.get("query_family"),
            "entity_route": cell.get("entity_route"),
            "as_of": cell.get("as_of"),
            "status": cell.get("status", "pending"),
            "note": cell.get("note"),
        }
        result = await poster("coverage_cells", row)
        if not result:
            raise RuntimeError("evidence-store: insert into coverage_cells returned no row")
        inserted.append(result[0])
    return inserted


async def store_raw_mention(
    *,
    coverage_cell_id: str,
    worker_result: dict[str, Any],
    post_fn: Poster | None = None,
) -> dict[str, Any]:
    """Store one acquisition-worker result as a raw_mentions row.

    Args:
        coverage_cell_id: The id of the coverage_cells row this mention
            belongs to (from store_coverage_cells).
        worker_result: The dict returned by the acquisition-worker skill
            (cell, query, findings_text, status, error).
        post_fn: Injectable poster for testing.

    Returns:
        The inserted raw_mentions row, including its generated id.

    Raises:
        RuntimeError: if the insert fails or returns no row.
    """
    poster = post_fn or _default_post
    row = {
        "coverage_cell_id": coverage_cell_id,
        "query_used": worker_result.get("query"),
        "findings_text": worker_result.get("findings_text"),
        "status": worker_result.get("status"),
        "error": worker_result.get("error"),
    }
    rows = await poster("raw_mentions", row)
    if not rows:
        raise RuntimeError("evidence-store: insert into raw_mentions returned no row")
    return rows[0]


async def run(
    *,
    action: str,
    **kwargs: Any,
) -> Any:
    """Dispatch entrypoint for the evidence-store skill.

    Args:
        action: One of "create_run", "store_coverage_cells", "store_raw_mention".
        **kwargs: Forwarded to the matching function above.

    Returns:
        Whatever the dispatched function returns.

    Raises:
        ValueError: if action is not a recognized value.
    """
    dispatch = {
        "create_run": create_run,
        "store_coverage_cells": store_coverage_cells,
        "store_raw_mention": store_raw_mention,
    }
    fn = dispatch.get(action)
    if fn is None:
        raise ValueError(
            f"evidence-store: unknown action '{action}', expected one of {sorted(dispatch)}"
        )
    return await fn(**kwargs)
