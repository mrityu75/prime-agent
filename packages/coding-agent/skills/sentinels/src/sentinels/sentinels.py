"""Sentinels skill implementation.

Independent omission search (SPEC.md Layers table, "Sentinels"): probes for
what the mandatory frontier (coverage-compiler cells) and the adaptive
frontier may have missed, and hands any new leads back to specialist workers.

Independence is the point (SPEC.md design rules 2 and 10). Sentinel probes
are built from policies that do not reuse the mandatory lattice's
query shape (query_family + modality + entity_route + region):

- raw_query_terms: terms the client wrote in the raw query that the
  structured contract and the mandatory cells never search for.
- lexical_variants: exact-code spellings of each target (PARP1 -> "PARP-1",
  "PARP 1"), per design rule 9.
- off_route_artifacts: artifact types outside the mandatory entity routes
  (posters, tech transfer, theses, grants, preprints, investor decks).
- uncovered_regions: regions/languages that no mandatory cell covers.

This module never reads gold data, never filters results with negative
phrasing, and never judges relevance. It only plans probes, executes them
through an injected search function, and reports which result URLs were
not already seen in the existing raw mentions.
"""

from __future__ import annotations

import hashlib
import random
import re
from typing import Any, Awaitable, Callable

SearchFn = Callable[[str], Awaitable[str]]

WEBSEARCH_FAILURE_SIGNALS = [
    "Web search is not set up yet",
    "Error searching for '",
]

POLICIES = ["raw_query_terms", "lexical_variants", "off_route_artifacts", "uncovered_regions"]

# Picks per interleaving round. The client's own raw-query terms are the
# strongest omission signal, so they get two picks per round. The total stays
# bounded by max_probes.
POLICY_WEIGHTS = {"raw_query_terms": 2, "lexical_variants": 1, "off_route_artifacts": 1, "uncovered_regions": 1}

OFF_ROUTE_ARTIFACTS = [
    "poster abstract",
    "tech transfer licensing opportunity",
    "thesis dissertation",
    "grant award",
    "preprint",
    "investor presentation pipeline",
]

SENTINEL_REGION_POOL = [
    {"region": "KR", "language": "ko"},
    {"region": "TW", "language": "zh"},
    {"region": "IN", "language": "en"},
    {"region": "DE", "language": "de"},
    {"region": "FR", "language": "fr"},
    {"region": "BR", "language": "pt"},
    {"region": "IL", "language": "en"},
    {"region": "AU", "language": "en"},
    {"region": "CA", "language": "en"},
    {"region": "UK", "language": "en"},
    {"region": "US", "language": "en"},
    {"region": "EU", "language": "en"},
    {"region": "JP", "language": "ja"},
    {"region": "CN", "language": "zh"},
]

URL_ROUTE_HINTS = [
    ("patents.google.", "patents"),
    ("patentscope", "patents"),
    ("espacenet", "patents"),
    ("uspto.gov", "patents"),
    ("clinicaltrials.gov", "trial_registries"),
    ("clinicaltrialsregister.eu", "trial_registries"),
    ("who.int/trialsearch", "trial_registries"),
    ("pubmed.", "literature"),
    ("pmc.ncbi.nlm.nih.gov", "literature"),
    ("ncbi.nlm.nih.gov/pmc", "literature"),
    ("pubs.acs.org", "literature"),
    ("nature.com", "literature"),
    ("sciencedirect.com", "literature"),
    ("cell.com", "literature"),
    ("ejcancer.com", "literature"),
    ("oncotarget.com", "literature"),
    ("biorxiv.org", "literature"),
    ("medrxiv.org", "literature"),
    ("sec.gov", "company_filings"),
    ("fda.gov", "regulatory_filings"),
    ("ema.europa.eu", "regulatory_filings"),
    ("aacrjournals.org", "conferences"),
    ("asco.org", "conferences"),
    ("meetings.", "conferences"),
    ("/abstracts", "conferences"),
]

_STOPWORDS = {
    "a", "an", "and", "any", "all", "as", "at", "by", "for", "from", "in", "into",
    "of", "on", "or", "over", "per", "the", "to", "with", "both", "including",
    "regardless", "currently", "effective", "assets", "asset", "drug", "drugs",
    "program", "programs", "therapeutic", "human", "family", "owner", "domicile",
    "active", "discontinued", "indication", "threshold",
}

_GENERIC_TERMS = {
    "selective", "selectivity", "inhibitor", "inhibitors", "inhibition", "inhibit", "potent", "novel",
    "compound", "compounds", "agent", "agents", "candidate", "candidates", "molecule", "molecules",
    "small", "therapy", "therapies", "treatment", "clinical", "phase", "stage", "stages", "development",
    "approved", "preclinical", "discovery", "modulator", "modulators", "direct", "indirect", "lead",
    "related", "relevant", "pathway", "pathways", "level", "broader", "close", "other", "global",
    "globally", "patients", "inactive", "registrational", "qualifying", "spanning", "across", "limited",
    "include", "exclude", "only", "based", "companies", "markets", "owned", "return", "identify",
    "population", "approach", "approaches", "targeting", "target", "targets", "directed", "restriction",
    "restricted", "geographic", "terminated", "shelved", "nontherapeutic", "tool", "natural", "products",
    "diagnostics", "nonlead", "indications", "manifestation", "variants", "core", "primary", "least",
    "filed", "reached", "antibody", "antibodies", "biologic", "biologics", "oligo", "oligos",
    "oligonucleotide", "oligonucleotides", "sirna", "sirnas", "aso", "asos", "adc", "adcs", "peptide",
    "peptides", "fda", "ind", "eu5", "programs", "program", "assets", "asset", "owner", "owners",
    "molecular", "results", "through", "while", "that", "have", "been", "which", "these", "this",
    "there", "their", "also", "more", "such", "each", "within", "without", "when", "where", "would",
    "could", "should", "than", "then", "them", "they", "united", "states", "america", "japan", "china",
    "korea", "europe", "european", "worldwide", "international", "cover", "limit", "same", "lead", "therapeutics", "therapeutic",
}
_GENERIC_STEMS = {w[:-1] if len(w) > 4 and w.endswith("s") else w for w in _GENERIC_TERMS}

_URL_RE = re.compile(r"^URL:\s*(\S+)", re.MULTILINE)
_TITLE_URL_RE = re.compile(r"^Result \d+:\s*(.*)\nURL:\s*(\S+)", re.MULTILINE)


def _scope(query_contract: dict[str, Any]) -> dict[str, Any]:
    return query_contract.get("scope") or {}


def _targets(query_contract: dict[str, Any]) -> list[str]:
    return list(_scope(query_contract).get("target_or_mechanism") or [])


def _mandatory_vocabulary(query_contract: dict[str, Any], cells: list[dict[str, Any]]) -> set[str]:
    """Words the mandatory queries actually search: each cell's query family, route, region,
    and source, plus the modality when exactly one is named (multi-modality contracts leave
    modality out of the queries). Other scope fields (indication, stage, exclusions, ...) are
    not searched by any mandatory cell, so terms there are still sentinel candidates.
    """
    words: set[str] = set()
    modalities = _scope(query_contract).get("modality") or []
    if isinstance(modalities, list) and len(modalities) == 1 and isinstance(modalities[0], str):
        words.update(w.lower() for w in re.findall(r"[A-Za-z0-9]+", modalities[0]))
    for cell in cells:
        for key in ("query_family", "entity_route", "region", "source"):
            words.update(w.lower() for w in re.findall(r"[A-Za-z0-9]+", str(cell.get(key) or "")))
    return words


def _stem(word: str) -> str:
    word = word.lower()
    return word[:-1] if len(word) > 4 and word.endswith("s") else word


def _is_symbol(term: str) -> bool:
    """Gene symbols and development codes: contains a digit, or two or more capitals (APOL1, VCP, PAMs)."""
    return any(ch.isdigit() for ch in term) or sum(ch.isupper() for ch in term) >= 2


def _term_priority(term: str) -> int:
    """0 for symbols/codes, 1 for other distinctive terms, 2 for generic qualifiers (searched last)."""
    if _stem(term) in _GENERIC_STEMS:
        return 2
    return 0 if _is_symbol(term) else 1


def _raw_query_terms(query_contract: dict[str, Any], cells: list[dict[str, Any]]) -> list[str]:
    """Raw-query terms no mandatory cell searches, most informative first.

    Gene symbols and codes come first (APOL1, VCP, SLC1A2), then other
    distinctive words, then generic qualifiers (_GENERIC_TERMS); within each
    group, longer terms come first, then query order. Three-letter tokens are
    kept only when they look like symbols.
    """
    raw = query_contract.get("raw_query") or ""
    covered = {_stem(w) for w in _mandatory_vocabulary(query_contract, cells)}
    seen: set[str] = set()
    terms: list[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9]+", raw):
        if len(token) < 4 and not (len(token) == 3 and _is_symbol(token)):
            continue
        stem = _stem(token)
        if token.lower() in _STOPWORDS or stem in covered or stem in seen:
            continue
        seen.add(stem)
        terms.append(token)
    order = {t: i for i, t in enumerate(terms)}
    return sorted(terms, key=lambda t: (_term_priority(t), -len(t), order[t]))


def _lexical_variants(target: str) -> list[str]:
    match = re.fullmatch(r"([A-Za-z]+)[\s\-]?(\d+[A-Za-z0-9]*)", target)
    if not match:
        return []
    letters, digits = match.groups()
    variants = [f"{letters}-{digits}", f"{letters} {digits}", f"{letters}{digits}"]
    return [f'"{v}"' for v in variants if v != target]


def _covered_regions(cells: list[dict[str, Any]]) -> set[str]:
    return {str(c.get("region")) for c in cells if c.get("region")}


def _used_queries(mentions: list[dict[str, Any]]) -> set[str]:
    used: set[str] = set()
    for m in mentions:
        q = m.get("query") or m.get("query_used")
        if q:
            used.add(q.strip().lower())
    return used


def _probe_id(policy: str, query: str) -> str:
    return hashlib.sha1(f"{policy}|{query}".encode()).hexdigest()[:12]


def plan_probes(
    *,
    query_contract: dict[str, Any],
    cells: list[dict[str, Any]],
    mentions: list[dict[str, Any]] | None = None,
    seed: int | None = None,
    max_probes: int = 12,
) -> list[dict[str, Any]]:
    """Plan sentinel probes that the mandatory frontier does not already run.

    Args:
        query_contract: The query contract (query-contract skill output).
        cells: Mandatory coverage cells (coverage-compiler output or
            evidence-store coverage_cells rows).
        mentions: Existing raw mentions (acquisition-worker results or
            evidence-store raw_mentions rows). Queries already run are skipped.
        seed: Selects a different subset/order per sweep, so repeated sweeps
            use fresh probes. None keeps the deterministic order.
        max_probes: Upper bound on returned probes. Probes are interleaved
            across policies so no single policy fills the budget.

    Returns:
        A list of probe dicts: probe_id, policy, query, rationale, region,
        language, as_of, status ("pending").

    Raises:
        ValueError: if query_contract has no scope.as_of_date or no targets.
    """
    scope = _scope(query_contract)
    as_of = scope.get("as_of_date")
    targets = _targets(query_contract)
    if not as_of:
        raise ValueError("sentinels: query_contract.scope.as_of_date is required")
    if not targets:
        raise ValueError("sentinels: query_contract.scope.target_or_mechanism is empty")

    modalities = scope.get("modality") or []
    modality = modalities[0] if isinstance(modalities, list) and len(modalities) == 1 else ""
    used = _used_queries(mentions or [])
    covered = _covered_regions(cells)
    by_policy: dict[str, list[dict[str, Any]]] = {p: [] for p in POLICIES}

    def add(
        policy: str,
        query: str,
        rationale: str,
        region: str | None = None,
        language: str | None = None,
        priority: int = 0,
    ) -> None:
        query = " ".join(query.split())
        if query.lower() in used:
            return
        used.add(query.lower())
        by_policy[policy].append(
            {
                "probe_id": _probe_id(policy, query),
                "policy": policy,
                "query": query,
                "rationale": rationale,
                "region": region,
                "language": language,
                "as_of": as_of,
                "status": "pending",
                "priority": priority,
            }
        )

    for target in targets:
        for term in _raw_query_terms(query_contract, cells):
            add(
                "raw_query_terms",
                f"{target} {term}",
                f"raw query term '{term}' is not searched by any mandatory cell",
                priority=_term_priority(term),
            )
        for variant in _lexical_variants(target):
            add("lexical_variants", f"{variant} {modality}", f"exact-code spelling {variant} of '{target}'")
        for artifact in OFF_ROUTE_ARTIFACTS:
            add("off_route_artifacts", f"{target} {modality} {artifact}", f"artifact type '{artifact}' is outside the mandatory entity routes")
        for info in SENTINEL_REGION_POOL:
            if info["region"] in covered:
                continue
            add(
                "uncovered_regions",
                f"{target} {modality} {info['region']}",
                f"region {info['region']} ({info['language']}) is not covered by any mandatory cell",
                region=info["region"],
                language=info["language"],
            )

    rng = random.Random(seed) if seed is not None else None
    if rng:
        for policy, probes in by_policy.items():
            if policy == "raw_query_terms":
                tiers: dict[int, list[dict[str, Any]]] = {}
                for p in probes:
                    tiers.setdefault(p["priority"], []).append(p)
                probes[:] = [p for t in sorted(tiers) for p in rng.sample(tiers[t], len(tiers[t]))]
            else:
                rng.shuffle(probes)

    planned: list[dict[str, Any]] = []
    while len(planned) < max_probes and any(by_policy.values()):
        for policy in POLICIES:
            for _ in range(POLICY_WEIGHTS[policy]):
                if by_policy[policy] and len(planned) < max_probes:
                    planned.append(by_policy[policy].pop(0))
    return planned


async def execute_probe(*, probe: dict[str, Any], websearch_fn: SearchFn | None = None) -> dict[str, Any]:
    """Run one sentinel probe through the injected search function.

    Args:
        probe: One probe dict from plan_probes.
        websearch_fn: The kernel's websearch callable (pass websearch_fn=websearch).

    Returns:
        A dict: probe, findings_text, status ("completed" or "failed"), error.
        Search failures (exceptions or websearch's text failure signals) are
        returned as "failed", never as an empty result.
    """
    if websearch_fn is None:
        return {"probe": probe, "findings_text": None, "status": "failed",
                "error": "sentinels: no websearch_fn provided. In the kernel, call with websearch_fn=websearch."}
    try:
        text = await websearch_fn(probe["query"])
    except Exception as e:  # noqa: BLE001 -- a failed lane is reported, never raised
        return {"probe": probe, "findings_text": None, "status": "failed", "error": f"sentinels: websearch call failed: {e}"}
    if any(signal in text for signal in WEBSEARCH_FAILURE_SIGNALS):
        return {"probe": probe, "findings_text": None, "status": "failed", "error": text}
    return {"probe": probe, "findings_text": text, "status": "completed", "error": None}


def _normalize_url(url: str) -> str:
    return url.rstrip("/").split("#")[0].lower()


def _suggest_route(url: str) -> str | None:
    low = url.lower()
    for hint, route in URL_ROUTE_HINTS:
        if hint in low:
            return route
    return None


def find_novel_leads(
    *,
    probe_results: list[dict[str, Any]],
    mentions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return result URLs from sentinel probes that no existing raw mention contains.

    Args:
        probe_results: execute_probe outputs. Failed probes are skipped.
        mentions: Existing raw mentions (acquisition-worker results or
            raw_mentions rows) whose findings_text defines what is already seen.

    Returns:
        One lead dict per new URL: url, title, probe_id, policy, query,
        suggested_route (entity route for a specialist worker, or None if
        the URL does not match a known source). Leads are not judged for
        relevance; that happens at admission.
    """
    seen: set[str] = set()
    for m in mentions or []:
        seen.update(_normalize_url(u) for u in _URL_RE.findall(m.get("findings_text") or ""))

    leads: list[dict[str, Any]] = []
    for result in probe_results:
        if result.get("status") != "completed":
            continue
        probe = result.get("probe") or {}
        for title, url in _TITLE_URL_RE.findall(result.get("findings_text") or ""):
            key = _normalize_url(url)
            if key in seen:
                continue
            seen.add(key)
            leads.append(
                {
                    "url": url,
                    "title": title.strip(),
                    "probe_id": probe.get("probe_id"),
                    "policy": probe.get("policy"),
                    "query": probe.get("query"),
                    "suggested_route": _suggest_route(url),
                }
            )
    return leads


async def run(*, action: str, **kwargs: Any) -> Any:
    """Dispatch entrypoint for the sentinels skill.

    Args:
        action: One of "plan_probes", "execute_probe", "find_novel_leads".
        **kwargs: Forwarded to the matching function.

    Returns:
        Whatever the dispatched function returns.

    Raises:
        ValueError: if action is not a recognized value.
    """
    dispatch: dict[str, Callable[..., Any]] = {
        "plan_probes": plan_probes,
        "execute_probe": execute_probe,
        "find_novel_leads": find_novel_leads,
    }
    fn = dispatch.get(action)
    if fn is None:
        raise ValueError(f"sentinels: unknown action '{action}', expected one of {sorted(dispatch)}")
    result = fn(**kwargs)
    if hasattr(result, "__await__"):
        result = await result
    return result
