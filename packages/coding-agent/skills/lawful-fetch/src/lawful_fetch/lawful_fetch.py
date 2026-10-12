"""Lawful-fetch skill implementation.

Fetches evidence documents through public APIs before falling back to a plain
GET, so pages that block automated access (403, captcha, JS shells) can still be
read lawfully: ClinicalTrials.gov API v2, Europe PMC (open-access full text and
abstracts), NCBI E-utilities (PubMed abstracts), Crossref (title -> DOI), and
Unpaywall (legal open-access copies, only when a contact email is configured).
Nothing here bypasses access controls: a blocked page is recorded as failed.

Every record carries a support level: "full_document" or "abstract", so the
admission gate can keep abstract-only evidence separate from full documents.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
MAX_CHARS = 60000


def _email() -> str:
    return os.environ.get("LAWFUL_FETCH_EMAIL", "").strip()


def _intervals() -> dict[str, float]:
    ncbi = 0.11 if os.environ.get("NCBI_API_KEY") else 0.34
    return {"ncbi": ncbi, "epmc": 0.5, "unpaywall": 0.5, "crossref": 1.0, "ctgov": 1.2, "web": 0.0}


class _Pacer:
    def __init__(self) -> None:
        self.locks: dict[str, asyncio.Lock] = {}
        self.last: dict[str, float] = {}

    async def wait(self, key: str) -> None:
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            gap = self.last.get(key, 0.0) + _intervals()[key] - time.monotonic()
            if gap > 0:
                await asyncio.sleep(gap)
            self.last[key] = time.monotonic()


_pacer = _Pacer()


async def _get(client: httpx.AsyncClient, key: str, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
    """Paced GET; a 429 is retried once after LAWFUL_FETCH_RETRY_DELAY seconds (default 10)."""
    delay = float(os.environ.get("LAWFUL_FETCH_RETRY_DELAY", "10"))
    resp = None
    for attempt in range(2):
        await _pacer.wait(key)
        resp = await client.get(url, params=params)
        if resp.status_code != 429 or attempt == 1:
            return resp
        await asyncio.sleep(delay)
    return resp  # type: ignore[return-value]


def _clean(text: str) -> str:
    return " ".join(text.split())[:MAX_CHARS]


def _xml_text(xml: str) -> str:
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return ""
    return _clean(" ".join(root.itertext()))


def _html_text(html: str) -> str:
    return _clean(BeautifulSoup(html, "lxml").get_text(" "))


def _blocked(text: str) -> bool:
    low = text.lower()
    return len(text) < 500 or "recaptcha" in low or "verify you are human" in low or "enable javascript" in low[:2000]


def _ct_text(j: dict[str, Any]) -> str:
    p = j.get("protocolSection", {})
    ident, status, design = p.get("identificationModule", {}), p.get("statusModule", {}), p.get("designModule", {})
    spons, conds, arms = p.get("sponsorCollaboratorsModule", {}), p.get("conditionsModule", {}), p.get("armsInterventionsModule", {})
    desc = p.get("descriptionModule", {})
    lines = [f"nctId: {ident.get('nctId')}", f"briefTitle: {ident.get('briefTitle')}", f"officialTitle: {ident.get('officialTitle')}",
             f"briefSummary: {' '.join((desc.get('briefSummary') or '').split())}", f"overallStatus: {status.get('overallStatus')}",
             f"studyType: {design.get('studyType')}", f"phases: {', '.join(design.get('phases') or [])}",
             f"leadSponsor: {(spons.get('leadSponsor') or {}).get('name')}", f"conditions: {', '.join(conds.get('conditions') or [])}"]
    for iv in arms.get("interventions") or []:
        other = f" ({', '.join(iv.get('otherNames') or [])})" if iv.get("otherNames") else ""
        lines.append(f"intervention: {iv.get('type')}: {iv.get('name')}{other}")
    for loc in (p.get("contactsLocationsModule", {}).get("locations") or [])[:200]:
        lines.append(f"location: {loc.get('city')}, {loc.get('state') or ''}, {loc.get('country')}")
    return "\n".join(lines)[:MAX_CHARS]


async def _epmc_search(client: httpx.AsyncClient, query: str) -> dict[str, Any] | None:
    r = await _get(client, "epmc", f"{EPMC}/search", {"query": query, "format": "json", "resultType": "core", "pageSize": 1})
    if r.status_code != 200:
        return None
    res = (r.json().get("resultList") or {}).get("result") or []
    return res[0] if res else None


async def _epmc_record(client: httpx.AsyncClient, hit: dict[str, Any], rec: dict[str, Any]) -> bool:
    """Full text for an open-access hit, else its abstract. Returns True on success."""
    pmcid = hit.get("pmcid")
    if pmcid and hit.get("isOpenAccess") == "Y":
        r = await _get(client, "epmc", f"{EPMC}/{pmcid}/fullTextXML")
        rec["steps"].append(f"europepmc fullTextXML {pmcid}: {r.status_code}")
        if r.status_code == 200:
            text = _xml_text(r.text)
            if len(text) > 1000:
                rec.update(ok=True, level="full_document", source_api="europepmc_fulltext", text=text, oa=True)
                return True
    abstract = _clean(BeautifulSoup(hit.get("abstractText") or "", "lxml").get_text(" "))
    if abstract:
        title = hit.get("title") or ""
        rec.update(ok=True, level="abstract", source_api="europepmc_abstract", text=_clean(f"{title} {abstract}"),
                   oa=hit.get("isOpenAccess") == "Y")
        return True
    return False


async def _pubmed_abstract(client: httpx.AsyncClient, pmid: str, rec: dict[str, Any]) -> bool:
    params = {"db": "pubmed", "id": pmid, "rettype": "abstract", "retmode": "text", "tool": "hrdss-lawful-fetch"}
    if _email():
        params["email"] = _email()
    if os.environ.get("NCBI_API_KEY"):
        params["api_key"] = os.environ["NCBI_API_KEY"]
    r = await _get(client, "ncbi", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi", params)
    rec["steps"].append(f"eutils efetch pubmed {pmid}: {r.status_code}")
    if r.status_code == 200 and len(r.text.strip()) > 200:
        rec.update(ok=True, level="abstract", source_api="eutils_pubmed", text=_clean(r.text), oa=False)
        return True
    return False


async def _by_doi(client: httpx.AsyncClient, doi: str, rec: dict[str, Any]) -> bool:
    hit = await _epmc_search(client, f'DOI:"{doi}"')
    rec["steps"].append(f"europepmc DOI {doi}: {'hit' if hit else 'none'}")
    if hit and hit.get("isOpenAccess") == "Y" and await _epmc_record(client, hit, rec) and rec["level"] == "full_document":
        return True
    if _email():
        r = await _get(client, "unpaywall", f"https://api.unpaywall.org/v2/{doi}", {"email": _email()})
        rec["steps"].append(f"unpaywall {doi}: {r.status_code}")
        if r.status_code == 200:
            loc = (r.json().get("best_oa_location") or {})
            oa_url = loc.get("url_for_landing_page") or loc.get("url")
            if oa_url and not oa_url.lower().endswith(".pdf"):
                page = await _get(client, "web", oa_url)
                if page.status_code == 200 and "pdf" not in page.headers.get("content-type", ""):
                    text = _html_text(page.text)
                    if not _blocked(text):
                        rec.update(ok=True, level="full_document", source_api="unpaywall_oa", text=text, oa=True)
                        return True
    return bool(hit) and await _epmc_record(client, hit, rec)


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2}


async def _title_to_doi(client: httpx.AsyncClient, title: str, rec: dict[str, Any]) -> str | None:
    params: dict[str, Any] = {"query.bibliographic": title, "rows": 1}
    if _email():
        params["mailto"] = _email()
    r = await _get(client, "crossref", "https://api.crossref.org/works", params)
    rec["steps"].append(f"crossref title: {r.status_code}")
    if r.status_code != 200:
        return None
    items = r.json().get("message", {}).get("items") or []
    if not items:
        return None
    found = " ".join(items[0].get("title") or [])
    a, b = _tokens(title), _tokens(found)
    return items[0].get("DOI") if a and len(a & b) / len(a) >= 0.7 else None


async def resolve(url: str, title: str | None = None, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Fetch one URL through the resolver chain. Never raises; failures are recorded.

    Returns: {"url", "ok", "level" ("full_document" | "abstract" | None), "source_api", "text", "oa",
    "error", "steps"}.
    """
    rec: dict[str, Any] = {"url": url, "ok": False, "level": None, "source_api": None, "text": None, "oa": None,
                           "error": None, "steps": []}
    own = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": UA})
    try:
        nct = re.search(r"(NCT\d{8})", url)
        if nct and "clinicaltrials.gov" in url:
            r = await _get(client, "ctgov", f"https://clinicaltrials.gov/api/v2/studies/{nct.group(1)}")
            rec["steps"].append(f"ctgov {nct.group(1)}: {r.status_code}")
            if r.status_code == 200:
                rec.update(ok=True, level="full_document", source_api="ctgov_v2", text=_ct_text(r.json()), oa=True)
                return rec
        pmc = re.search(r"(PMC\d+)", url)
        if pmc and ("ncbi.nlm.nih.gov" in url or "europepmc" in url):
            hit = await _epmc_search(client, f"PMCID:{pmc.group(1)}")
            if hit and await _epmc_record(client, {**hit, "pmcid": pmc.group(1)}, rec):
                return rec
        pmid = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)|europepmc\.org/(?:abstract|article)/med/(\d+)", url, re.I)
        if pmid:
            pid = pmid.group(1) or pmid.group(2)
            hit = await _epmc_search(client, f"EXT_ID:{pid} AND SRC:MED")
            if hit and await _epmc_record(client, hit, rec):
                return rec
            if await _pubmed_abstract(client, pid, rec):
                return rec
        doi = re.search(r"(10\.\d{4,9}/[^\s?#]+)", url)
        if doi and await _by_doi(client, doi.group(1).rstrip("/").removesuffix("/full").removesuffix("/abstract"), rec):
            return rec
        try:
            page = await _get(client, "web", url)
            rec["steps"].append(f"GET: {page.status_code}")
            if page.status_code == 200 and "pdf" not in page.headers.get("content-type", ""):
                text = _html_text(page.text)
                if not _blocked(text):
                    rec.update(ok=True, level="full_document", source_api="web", text=text)
                    return rec
                rec["error"] = "bot check, JS shell, or empty page"
            else:
                rec["error"] = f"HTTP {page.status_code}" if page.status_code != 200 else "pdf not parsed"
        except httpx.HTTPError as e:
            rec["error"] = repr(e)[:200]
        if title:
            clean_title = re.sub(r"\s*(\.\.\.|…)\s*$", "", title).strip()
            if len(clean_title) > 15:
                found = await _title_to_doi(client, clean_title, rec)
                if found and await _by_doi(client, found, rec):
                    rec["error"] = None
                    return rec
                hit = await _epmc_search(client, f'TITLE:"{clean_title[:120]}"')
                rec["steps"].append(f"europepmc title: {'hit' if hit else 'none'}")
                if hit and await _epmc_record(client, hit, rec):
                    rec["error"] = None
                    return rec
        return rec
    except Exception as e:  # noqa: BLE001 -- a failed fetch is recorded, never raised
        rec["error"] = repr(e)[:200]
        return rec
    finally:
        if own:
            await client.aclose()


async def fetch_all(*, items: list[dict[str, str]], concurrency: int = 4) -> list[dict[str, Any]]:
    """Resolve many {"url", "title"?} items. Per-API pacing is shared across all of them."""
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": UA}) as client:
        async def one(it: dict[str, str]) -> dict[str, Any]:
            async with sem:
                return await resolve(it["url"], it.get("title"), client)
        return await asyncio.gather(*[one(it) for it in items])


async def run(*, action: str, **kwargs: Any) -> Any:
    """Dispatch entrypoint: action is "resolve" (url, title) or "fetch_all" (items)."""
    if action == "resolve":
        return await resolve(kwargs["url"], kwargs.get("title"))
    if action == "fetch_all":
        return await fetch_all(**kwargs)
    raise ValueError(f"lawful-fetch: unknown action '{action}', expected one of ['fetch_all', 'resolve']")
