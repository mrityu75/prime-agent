"""Verdict-queue skill implementation.

Runs admission verdicts as bounded, resumable units driven by the orchestrator:
one direct model call per candidate (or small batch), never one long agent turn.
The verdict model judges each criterion from numbered evidence quotes and cites a
quote by index; this module copies the exact quote, URL, and mention id into the
claim, so every claim quote is verbatim by construction.

Every candidate keeps an outcome: "judged", "content_filtered" (the provider's
safety filter returned no output), "parse_failed", or "error". Blocked candidates
stay in the queue, flagged, and are never sent to a fallback model.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

DEFAULT_MODEL = "global.anthropic.claude-opus-5-5"
Converse = Callable[[str, str, str], Awaitable[dict[str, Any]]]

SYSTEM = (
    "You are the verdict step of a drug-sourcing admission pipeline. You judge each criterion for each "
    "candidate strictly from the numbered evidence quotes given. You never use outside knowledge to decide "
    "a verdict, never invent quotes, and output only JSON."
)


async def bedrock_converse(model_id: str, system: str, user: str) -> dict[str, Any]:
    """Call Amazon Bedrock Converse with AWS_BEARER_TOKEN_BEDROCK. Returns stop_reason, text, usage."""
    token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")
    region = os.environ.get("AWS_REGION", "us-west-2")
    if not token:
        raise RuntimeError("verdict-queue: AWS_BEARER_TOKEN_BEDROCK not set")
    url = f"https://bedrock-runtime.{region}.amazonaws.com/model/{urllib.parse.quote(model_id, safe='')}/converse"
    body = {"system": [{"text": system}], "messages": [{"role": "user", "content": [{"text": user}]}],
            "inferenceConfig": {"maxTokens": 8000}}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    last = ""
    for attempt in range(3):
        async with httpx.AsyncClient(timeout=600) as client:
            resp = await client.post(url, json=body, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            text = "".join(b.get("text", "") for b in data["output"]["message"]["content"] if "text" in b)
            return {"stop_reason": data.get("stopReason"), "text": text, "usage": data.get("usage") or {}}
        last = f"HTTP {resp.status_code}: {resp.text[:200]}"
        if resp.status_code != 429 and resp.status_code < 500:
            break
        await asyncio.sleep(10 * (attempt + 1))
    raise RuntimeError(f"verdict-queue: Bedrock call failed: {last}")


def _parse_json(text: str | None) -> Any:
    if not text:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j < 0:
        return None
    try:
        return json.loads(t[i:j + 1])
    except ValueError:
        return None


def _load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _save(path: Path, items: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(i, default=str) + "\n" for i in items))
    tmp.replace(path)


def build_queue(*, path: str, packets: dict[str, list[dict[str, Any]]], order: list[str] | None = None) -> dict[str, Any]:
    """Create a queue file with one pending item per candidate.

    Args:
        path: Queue file (JSONL). Must not exist yet.
        packets: candidate name -> evidence quotes (dicts with quote, url, mention_id, and optional about,
            quote_names_candidate), e.g. the cheap-model extraction output.
        order: Optional priority order of candidate names; others follow in packet order.

    Returns:
        {"path", "items"}.
    """
    p = Path(path).expanduser()
    if p.exists():
        raise ValueError(f"verdict-queue: {p} already exists")
    names = [n for n in (order or []) if n in packets] + [n for n in packets if n not in set(order or [])]
    items = [{"candidate": n, "packet": packets[n], "status": "pending", "attempts": 0, "claims": [], "detail": None}
             for n in names]
    p.parent.mkdir(parents=True, exist_ok=True)
    _save(p, items)
    return {"path": str(p), "items": len(items)}


def _prompt(spec: dict[str, Any], names: list[str], packets: dict[str, list[dict[str, Any]]]) -> str:
    lines = []
    for n in names:
        lines.append(f"### CANDIDATE: {n}")
        for i, q in enumerate(packets[n]):
            lines.append(f"[{i}] (about={q.get('about')}, names_candidate={q.get('quote_names_candidate')}) {q['quote']}")
    rules = spec.get("inference_rules") or []
    rule_text = "\n".join(f'- id "{r["id"]}" for criterion "{r["criterion"]}": {r["statement"]}' for r in rules) or "- none"
    return f"""Raw query:
{spec['raw_query']}

Criteria (use these strings exactly): {json.dumps(spec['criteria'])}
Required output fields (field claims with "value"): {json.dumps(spec.get('required_fields') or [])}
Scope exclusions (criterion string exactly; verdict "met" only if a quote shows the candidate matches): {json.dumps(spec.get('exclusions') or [])}
Declared inference rules (basis "inferred" with inference_rule id; verdict "met" only; only for that criterion):
{rule_text}
Sponsor-domicile criterion "sponsor_constraints" (if listed): one claim per party with "party" = originator, current_owner, originator_and_current_owner, licensee, or sponsor.

For each candidate write claims: one per criterion you have evidence for (verdict "met", "not_met", or "unclear", citing ONE quote by index), field claims with "value" when a quote supports it, and an exclusion claim only when a quote shows a match. Use quotes with names_candidate=false only if the quote clearly refers to this candidate. Judge strictly from the quote text; skip criteria with no relevant quote.

Return {{"claims": [{{"candidate": <name exactly as given>, "criterion": ..., "verdict": ..., "quote_index": <int>, "value": <fields only>, "basis": "stated"|"inferred", "inference_rule": <id or null>, "party": <or null>}}]}}.

{chr(10).join(lines)}"""


def _to_claims(raw: Any, names: list[str], packets: dict[str, list[dict[str, Any]]], model: str) -> tuple[list[dict[str, Any]], int]:
    out: list[dict[str, Any]] = []
    bad = 0
    for c in raw.get("claims", []) if isinstance(raw, dict) else []:
        n, i = c.get("candidate"), c.get("quote_index")
        if n not in names or not isinstance(i, int) or not 0 <= i < len(packets[n]):
            bad += 1
            continue
        q = packets[n][i]
        claim: dict[str, Any] = {"candidate": n, "criterion": c.get("criterion"), "verdict": c.get("verdict"),
                                 "quote": q["quote"], "source_url": q.get("url"), "verdict_model": model}
        if q.get("mention_id"):
            claim["mention_id"] = q["mention_id"]
        for key in ("value", "party", "inference_rule"):
            if c.get(key):
                claim[key] = c[key]
        if c.get("basis") == "inferred":
            claim["basis"] = "inferred"
        out.append(claim)
    return out, bad


async def process(
    *,
    path: str,
    spec: dict[str, Any],
    model: str = DEFAULT_MODEL,
    batch_size: int = 1,
    concurrency: int = 4,
    max_items: int | None = None,
    filtered_retries: int = 2,
    converse_fn: Converse | None = None,
) -> dict[str, Any]:
    """Judge pending (and retryable) queue items; saves after every unit, so it can be re-run to resume.

    Args:
        path: Queue file from build_queue.
        spec: {"raw_query", "criteria", "required_fields", "exclusions", "inference_rules"}.
        model: Verdict model id. Blocked items are never re-sent to a different model.
        batch_size: Candidates per call (1 isolates content-filter blocks to one candidate).
        concurrency: Parallel calls.
        max_items: Stop after this many units in this call (bounded work per invocation).
        filtered_retries: Extra attempts for items blocked by the content filter (the filter is not deterministic).
        converse_fn: Injectable (model, system, user) -> {"stop_reason", "text", "usage"} for tests.

    Returns:
        status() counts after processing, plus "usage" token totals for this call.
    """
    qpath = Path(path).expanduser()
    items = _load(qpath)
    call = converse_fn or bedrock_converse
    todo = [i for i, it in enumerate(items)
            if it["status"] == "pending" or (it["status"] == "content_filtered" and it["attempts"] <= filtered_retries)
            or (it["status"] in ("parse_failed", "error") and it["attempts"] < 2)]
    units = [todo[k:k + batch_size] for k in range(0, len(todo), batch_size)]
    if max_items is not None:
        units = units[:max_items]
    usage = {"input_tokens": 0, "output_tokens": 0}
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()

    async def run_unit(idx: list[int]) -> None:
        names = [items[i]["candidate"] for i in idx]
        packets = {items[i]["candidate"]: items[i]["packet"] for i in idx}
        async with sem:
            try:
                resp = await call(model, SYSTEM, _prompt(spec, names, packets))
                status, detail, claims = "judged", None, []
                if resp.get("stop_reason") == "content_filtered":
                    status, detail = "content_filtered", "provider safety filter returned no output"
                else:
                    raw = _parse_json(resp.get("text"))
                    if raw is None:
                        status, detail = "parse_failed", (resp.get("text") or "")[:200]
                    else:
                        claims, bad = _to_claims(raw, names, packets, model)
                        detail = f"{bad} claims with bad candidate/quote_index dropped" if bad else None
                u = resp.get("usage") or {}
                usage["input_tokens"] += u.get("inputTokens", 0)
                usage["output_tokens"] += u.get("outputTokens", 0)
            except Exception as e:  # noqa: BLE001 -- recorded on the item, never raised
                status, detail, claims = "error", repr(e)[:200], []
        async with lock:
            for i in idx:
                items[i]["attempts"] += 1
                items[i]["status"] = status
                items[i]["detail"] = detail
                items[i]["model"] = model
                items[i]["claims"] = [c for c in claims if c["candidate"] == items[i]["candidate"]]
            _save(qpath, items)

    await asyncio.gather(*[run_unit(u) for u in units])
    return {**status(path=path), "usage": usage}


def status(*, path: str) -> dict[str, Any]:
    """Counts per outcome, plus the names of content-filtered candidates."""
    items = _load(Path(path).expanduser())
    counts: dict[str, int] = {}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    return {"items": len(items), "counts": counts,
            "content_filtered": [it["candidate"] for it in items if it["status"] == "content_filtered"]}


def claims(*, path: str) -> list[dict[str, Any]]:
    """All claims from judged items, ready for admission_gate evaluate."""
    return [c for it in _load(Path(path).expanduser()) if it["status"] == "judged" for c in it["claims"]]


async def run(*, action: str, **kwargs: Any) -> Any:
    """Dispatch entrypoint: action is one of "build_queue", "process", "status", "claims"."""
    if action == "build_queue":
        return build_queue(**kwargs)
    if action == "process":
        return await process(**kwargs)
    if action == "status":
        return status(**kwargs)
    if action == "claims":
        return claims(**kwargs)
    raise ValueError(f"verdict-queue: unknown action '{action}', expected one of ['build_queue', 'claims', 'process', 'status']")
