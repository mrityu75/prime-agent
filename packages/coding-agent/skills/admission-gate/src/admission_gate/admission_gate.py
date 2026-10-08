"""Admission-gate skill implementation.

The judgment layer (SPEC.md Layers table, "Screening & admission"): a
separate admission service evaluates each atomic criterion and renders the
final table deterministically from supported claims.

Rules this module enforces (SPEC.md design rules and CLAUDE.md conventions):

- No live model judgment. The calling agent extracts claims; this module
  only checks that each claim is supported by stored, citable text and then
  applies fixed rules. Same inputs always give the same output.
- A claim is supported only if its quote appears verbatim (case and
  whitespace normalized) in the cited raw mention's findings_text, or in a
  supplied full document for its source_url, and the source_url appears in
  that mention when a mention is cited.
- Snippets are leads, not qualification evidence. When the contract requires
  full documents, snippet-only support sends a candidate to review, never
  to admission.
- Absence of evidence is never rejection. Only a supported "not_met" claim
  (or a supported "met" on an exclusion) rejects a candidate.
- Nothing is deleted (design rule 4). Every candidate appears in exactly one
  bucket with its reasons.
- Never merge (design rule 6). Names that normalize to the same key are
  reported as reversible possible_same_as links, not merged.
- Status is never "complete". This module can only report "incomplete" or
  "blocked"; provisional saturation needs stopping checks it cannot run.
"""

from __future__ import annotations

import os
import re
from typing import Any, Awaitable, Callable

import httpx

Getter = Callable[[str, dict[str, str]], Awaitable[list[dict[str, Any]]]]

VERDICTS = {"met", "not_met", "unclear"}
SCOPE_CRITERIA = ["target_or_mechanism", "modality", "indication", "stage", "geography", "sponsor_constraints"]
CLAIM_FIELDS = ["candidate", "criterion", "verdict", "quote", "source_url"]

_URL_RE = re.compile(r"^URL:\s*(\S+)", re.MULTILINE)


def _norm_text(text: str) -> str:
    return " ".join(text.lower().split())


def _norm_url(url: str) -> str:
    return url.strip().rstrip("/").split("#")[0].lower()


def _identity_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


async def _default_get(table: str, params: dict[str, str]) -> list[dict[str, Any]]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SECRET_KEY", "")
    if not url or not key:
        raise RuntimeError("admission-gate: SUPABASE_URL or SUPABASE_SECRET_KEY not set in the environment")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{url}/rest/v1/{table}",
            params=params,
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
        )
        resp.raise_for_status()
        return resp.json()


async def load_run(*, run_id: str, get_fn: Getter | None = None) -> dict[str, Any]:
    """Read one run and its coverage cells and raw mentions from Supabase.

    Args:
        run_id: The runs.id written by evidence-store's create_run.
        get_fn: Injectable reader for testing; defaults to a PostgREST GET.

    Returns:
        {"run": <runs row>, "cells": [<coverage_cells rows>],
         "mentions": [<raw_mentions rows>]}.

    Raises:
        RuntimeError: if the run does not exist, the environment is not set,
            or a request fails.
    """
    getter = get_fn or _default_get
    runs = await getter("runs", {"select": "*", "id": f"eq.{run_id}"})
    if not runs:
        raise RuntimeError(f"admission-gate: run '{run_id}' not found")
    cells = await getter("coverage_cells", {"select": "*", "run_id": f"eq.{run_id}", "order": "created_at.asc"})
    mentions: list[dict[str, Any]] = []
    if cells:
        ids = ",".join(str(c["id"]) for c in cells)
        mentions = await getter("raw_mentions", {"select": "*", "coverage_cell_id": f"in.({ids})", "order": "created_at.asc"})
    return {"run": runs[0], "cells": cells, "mentions": mentions}


def _criteria_from_contract(query_contract: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    scope = query_contract.get("scope") or {}
    evidence = query_contract.get("evidence") or {}
    criteria = [c for c in SCOPE_CRITERIA if scope.get(c)]
    exclusions = list(scope.get("exclusions") or [])
    fields = list(evidence.get("required_output_fields") or [])
    return criteria, exclusions, fields


def _check_claim(
    claim: dict[str, Any],
    mentions_by_id: dict[str, dict[str, Any]],
    documents: dict[str, str],
) -> tuple[str | None, str | None]:
    """Return (support level, problem). Support level is "full_document", "snippet", or None."""
    missing = [f for f in CLAIM_FIELDS if not claim.get(f)]
    if missing:
        return None, f"missing claim fields: {', '.join(missing)}"
    if claim["verdict"] not in VERDICTS:
        return None, f"verdict '{claim['verdict']}' not in {sorted(VERDICTS)}"
    quote = _norm_text(claim["quote"])
    url = _norm_url(claim["source_url"])

    doc = {_norm_url(u): t for u, t in documents.items()}.get(url)
    if doc is not None and quote in _norm_text(doc):
        return "full_document", None

    mention_id = claim.get("mention_id")
    if mention_id is None:
        return None, "quote not found in a supplied document and no mention_id cited"
    mention = mentions_by_id.get(str(mention_id))
    if mention is None:
        return None, f"mention_id '{mention_id}' is not a stored raw mention"
    text = mention.get("findings_text") or ""
    if url not in {_norm_url(u) for u in _URL_RE.findall(text)}:
        return None, "source_url does not appear in the cited mention"
    if quote not in _norm_text(text):
        return None, "quote does not appear in the cited mention"
    return "snippet", None


def _coverage_report(
    cells: list[dict[str, Any]], mentions: list[dict[str, Any]], admitted: list[dict[str, Any]]
) -> dict[str, Any]:
    by_cell: dict[str, list[str]] = {}
    for m in mentions:
        by_cell.setdefault(str(m.get("coverage_cell_id")), []).append(m.get("status") or "")
    lanes = []
    counts = {"completed": 0, "failed": 0, "pending": 0}
    for c in cells:
        statuses = by_cell.get(str(c.get("id")), [])
        if "completed" in statuses:
            lane = "completed"
        elif statuses:
            lane = "failed"
        else:
            lane = "pending"
        counts[lane] += 1
        lanes.append({"cell_id": c.get("id"), "entity_route": c.get("entity_route"), "region": c.get("region"),
                      "language": c.get("language"), "query_family": c.get("query_family"), "lane_status": lane})
    if cells and counts["completed"] == 0 and counts["pending"] == 0:
        status = "blocked"
    else:
        status = "incomplete"
    return {
        "status": status,
        "lane_counts": counts,
        "lanes": lanes,
        "saturation_checks": {
            "1_all_mandatory_cells_completed_or_blocked": bool(cells) and counts["pending"] == 0,
            "2_identity_gaps_resolved": "not_evaluated",
            "3_independent_sweeps_and_sentinels_found_nothing_new": "not_evaluated",
            "4_residual_miss_model_and_audit": "not_evaluated",
            "5_no_admitted_row_lacks_required_evidence": all(not row["reasons"] for row in admitted),
        },
        "note": "admission-gate never reports provisional saturation; stopping checks 2-4 run elsewhere",
    }


def evaluate(
    *,
    query_contract: dict[str, Any],
    claims: list[dict[str, Any]],
    mentions: list[dict[str, Any]],
    cells: list[dict[str, Any]] | None = None,
    documents: dict[str, str] | None = None,
    criteria: list[str] | None = None,
) -> dict[str, Any]:
    """Decide admission for every candidate named in the claims.

    Args:
        query_contract: The query contract. Supplies the atomic criteria
            (non-empty scope fields), exclusions, required output fields,
            full_document_required, and ambiguity_policy.
        claims: Claim dicts extracted by the calling agent, one per
            (candidate, criterion) observation: candidate, criterion,
            verdict ("met", "not_met", "unclear"), quote (verbatim text),
            source_url, optional mention_id (raw_mentions id or the index
            key used in mentions), optional value (required for output fields).
        mentions: Stored raw mentions (load_run output or rows with id and
            findings_text). The citable evidence for snippet-level claims.
        cells: Coverage cells, for the coverage report.
        documents: Optional {source_url: full text} the agent read. A quote
            found here counts as full-document support.
        criteria: Override the criteria derived from the contract.

    Returns:
        {"admitted", "review", "candidates", "rejected"} lists of candidate
        rows, plus "unsupported_claims", "possible_same_as", and
        "coverage_report". Each row: candidate, decision, reasons,
        criteria (per-criterion verdict and support), fields, citations.
    """
    derived, exclusions, fields = _criteria_from_contract(query_contract)
    criteria = list(criteria) if criteria is not None else derived
    evidence = query_contract.get("evidence") or {}
    full_required = evidence.get("full_document_required", True)
    policy = (query_contract.get("semantics") or {}).get("ambiguity_policy", "include_and_flag")
    mentions_by_id = {str(m.get("id")): m for m in mentions}
    documents = documents or {}

    supported: dict[str, list[dict[str, Any]]] = {}
    unsupported: list[dict[str, Any]] = []
    for claim in claims:
        level, problem = _check_claim(claim, mentions_by_id, documents)
        if level is None:
            unsupported.append({"claim": claim, "problem": problem})
            continue
        supported.setdefault(claim["candidate"].strip(), []).append({**claim, "support": level})

    buckets: dict[str, list[dict[str, Any]]] = {"admitted": [], "review": [], "candidates": [], "rejected": []}
    names = set(supported) | {c["candidate"].strip() for c in claims if isinstance(c.get("candidate"), str) and c["candidate"].strip()}
    for name in sorted(names):
        rows = supported.get(name, [])
        reasons: list[str] = []
        per_criterion: dict[str, dict[str, Any]] = {}
        missing = 0
        reject = False
        review = False

        for criterion in criteria:
            cl = [r for r in rows if r["criterion"] == criterion]
            verdicts = {r["verdict"] for r in cl}
            levels = {r["support"] for r in cl if r["verdict"] == "met"}
            per_criterion[criterion] = {"verdicts": sorted(verdicts), "met_support": sorted(levels)}
            if "met" in verdicts and "not_met" in verdicts:
                review = True
                reasons.append(f"{criterion}: contradictory supported claims")
            elif "not_met" in verdicts:
                reject = True
                reasons.append(f"{criterion}: supported not_met")
            elif "met" in verdicts:
                if full_required and "full_document" not in levels:
                    review = True
                    reasons.append(f"{criterion}: met only in snippets; full document required")
            elif "unclear" in verdicts:
                if policy == "exclude":
                    reject = True
                    reasons.append(f"{criterion}: unclear and ambiguity_policy is exclude")
                else:
                    review = True
                    reasons.append(f"{criterion}: unclear (include_and_flag)")
            else:
                missing += 1
                reasons.append(f"{criterion}: no supported claim")

        for exclusion in exclusions:
            if any(r["criterion"] == exclusion and r["verdict"] == "met" for r in rows):
                reject = True
                reasons.append(f"exclusion '{exclusion}': supported met")

        field_values: dict[str, Any] = {}
        for f in fields:
            vals = sorted({str(r["value"]) for r in rows if r["criterion"] == f and r["verdict"] == "met" and r.get("value")})
            field_values[f] = vals[0] if len(vals) == 1 else (vals or None)
            if not vals:
                missing += 1
                reasons.append(f"field '{f}': no supported value")
            elif len(vals) > 1:
                review = True
                reasons.append(f"field '{f}': conflicting values {vals}")
            elif full_required and not any(
                r["support"] == "full_document" for r in rows if r["criterion"] == f and r["verdict"] == "met"
            ):
                review = True
                reasons.append(f"field '{f}': value only in snippets; full document required")

        if reject:
            decision = "rejected"
        elif missing == 0 and not review:
            decision = "admitted"
        elif missing <= 1:
            decision = "review"
        else:
            decision = "candidates"

        citations = sorted(
            ({"criterion": r["criterion"], "quote": r["quote"], "source_url": r["source_url"],
              "mention_id": r.get("mention_id"), "support": r["support"]} for r in rows),
            key=lambda c: (c["criterion"], c["source_url"], c["quote"]),
        )
        buckets[decision].append({"candidate": name, "decision": decision, "reasons": reasons,
                                "criteria": per_criterion, "fields": field_values, "citations": citations})

    groups: dict[str, set[str]] = {}
    for claim in claims:
        if claim.get("candidate"):
            groups.setdefault(_identity_key(claim["candidate"]), set()).add(claim["candidate"].strip())
    same_as = [sorted(g) for g in groups.values() if len(g) > 1]

    return {
        **buckets,
        "unsupported_claims": unsupported,
        "possible_same_as": sorted(same_as),
        "coverage_report": _coverage_report(cells or [], mentions, buckets["admitted"]),
    }


async def run(*, action: str, **kwargs: Any) -> Any:
    """Dispatch entrypoint for the admission-gate skill.

    Args:
        action: One of "load_run", "evaluate".
        **kwargs: Forwarded to the matching function.

    Returns:
        Whatever the dispatched function returns.

    Raises:
        ValueError: if action is not a recognized value.
    """
    if action == "load_run":
        return await load_run(**kwargs)
    if action == "evaluate":
        return evaluate(**kwargs)
    raise ValueError(f"admission-gate: unknown action '{action}', expected one of ['evaluate', 'load_run']")
