---
name: lawful-fetch
description: Fetch evidence documents for the drug-sourcing pipeline through public APIs (ClinicalTrials.gov v2, Europe PMC, NCBI E-utilities, Crossref, Unpaywall) before a plain GET, with per-API pacing, and tag each result as full_document or abstract for admission-gate.
---

# Lawful Fetch

Many evidence pages block automated access (403, captcha, JavaScript-only
pages). This skill reads them lawfully through public APIs instead, and never
bypasses access controls: a page that is still blocked is recorded as failed
(SPEC.md design rule 12).

Resolver chain per URL:
1. ClinicalTrials.gov study URL -> API v2 record (full_document).
2. PMC URL -> Europe PMC open-access full text, else its abstract.
3. PubMed / Europe PMC MED URL -> Europe PMC (open-access full text or
   abstract), else NCBI E-utilities abstract.
4. URL with a DOI -> Europe PMC open-access full text; else an Unpaywall legal
   open-access copy (only when LAWFUL_FETCH_EMAIL is set); else the abstract.
5. Plain GET (full_document when the page has real content).
6. If the GET fails and a result title is given: Crossref title -> DOI ->
   step 4, else Europe PMC title search -> abstract.

Each record: url, ok, level ("full_document" or "abstract"), source_api,
text, oa, error, steps. Pass full documents to admission-gate as `documents`
and abstracts as `abstracts`.

Pacing per API (shared across calls in one kernel): NCBI 3 req/s (10 with
NCBI_API_KEY), Europe PMC and Unpaywall 2 req/s, Crossref 1 req/s,
ClinicalTrials.gov about 1 req/s. A 429 is retried once after
LAWFUL_FETCH_RETRY_DELAY seconds (default 10).

Environment: LAWFUL_FETCH_EMAIL (contact email for E-utilities, Crossref,
and Unpaywall; Unpaywall is skipped without it), NCBI_API_KEY (optional).

## Usage

```python
records = await lawful_fetch(action="fetch_all", items=[{"url": u, "title": t} for u, t in urls])
documents = {r["url"]: r["text"] for r in records if r["level"] == "full_document"}
abstracts = {r["url"]: r["text"] for r in records if r["level"] == "abstract"}
```
