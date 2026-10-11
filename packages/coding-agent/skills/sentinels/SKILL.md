---
name: sentinels
description: Independent omission search for the drug-sourcing pipeline. Plans sentinel probes that the mandatory coverage cells do not run, executes them via web search, and returns result URLs not already seen in existing raw mentions as new leads for specialist workers, per SPEC.md's Sentinels layer.
---

# Sentinels

Probes for what the mandatory frontier (coverage-compiler cells) and the
adaptive frontier may have missed, and hands new leads back to specialist
workers (SPEC.md Layers table, "Sentinels"). Runs independently of Jev.

Independence is the point (SPEC.md design rules 2 and 10). Probes come from
policies that do not reuse the mandatory lattice's query shape
(query_family + modality + entity_route + region):

| Policy | What it probes |
|---|---|
| `raw_query_terms` | Terms in the client's raw query that no mandatory cell searches for (e.g. "trappers", "allosteric", "PARP2") |
| `lexical_variants` | Exact-code spellings of each target ("PARP-1", "PARP 1"), per design rule 9 |
| `off_route_artifacts` | Artifact types outside the mandatory entity routes: posters, tech transfer, theses, grants, preprints, investor decks |
| `uncovered_regions` | Regions that no mandatory cell covers (KR, TW, IN, DE, FR, BR, IL, AU, CA, ...) |

This skill never reads gold data (design rule 11), never uses negative
phrasing at retrieval (rule 5), and never judges relevance. Leads go to
specialist workers and then to admission.

## Data it reads

It works on in-memory lists in the same shapes evidence-store writes:

- `cells`: coverage-compiler output, or `coverage_cells` rows (region,
  query_family, entity_route, source, ...).
- `mentions`: acquisition-worker results (`query`, `findings_text`) or
  `raw_mentions` rows (`query_used`, `findings_text`). Queries already run
  are not planned again, and URLs already in `findings_text` are not
  reported as new.

It does not read Supabase directly. evidence-store has no read actions yet,
and the live schema is not confirmed.

## How to use this

Call `sentinels(action=..., **kwargs)`:

1. `action="plan_probes"` with `query_contract`, `cells`, optional
   `mentions`, `seed`, `max_probes` (default 12). Returns probe dicts:
   probe_id, policy, query, rationale, region, language, as_of, status
   ("pending"). Probes are interleaved across policies so one policy cannot
   fill the budget; `raw_query_terms` gets two picks per round because the
   client's own terms are the strongest omission signal. Raw-query terms are
   ranked most informative first: distinctive terms (e.g. "allosteric",
   "trappers") before generic qualifiers (e.g. "selective", "inhibitor"),
   longer before shorter. Pass a different `seed` per sweep to get fresh probes
   (SPEC.md stopping criterion 3: "fresh sentinel policies").
2. `action="execute_probe"` with `probe` and `websearch_fn=websearch`.
   Returns probe, findings_text, status ("completed" or "failed"), error.
   Search failures, including websearch's text failure signals, return
   "failed" -- log them as failed lanes, never as "no results".
3. `action="find_novel_leads"` with `probe_results` and `mentions`. Returns
   one lead per new URL: url, title, probe_id, policy, query,
   suggested_route (patents, literature, trial_registries, company_filings,
   regulatory_filings, conferences, or None when the URL does not match a
   known source). Send leads to the matching specialist worker.

Run probes in parallel via subagents for throughput; this skill does not
parallelize.

Known limit: `uncovered_regions` probes use the English target name with a
region hint. There is no translation into native terms or scripts yet, so
these probes do not yet meet design rule 9's native-script lanes.

## Usage

```python
contract = await query_contract(
    raw_query="Human PARP1-selective small molecule inhibitors, including PARP1 trappers and allosteric modulators, with a US IND",
    scope={"target_or_mechanism": ["PARP1"], "modality": ["small molecule"], "as_of_date": "2026-09-29"},
    evidence={"required_output_fields": ["Drug Name", "Sponsor", "Development Stage"]},
)
cells = await coverage_compiler(query_contract=contract)
mentions = [await acquisition_worker(cell=cells[0], query_contract=contract, websearch_fn=websearch)]

probes = await sentinels(action="plan_probes", query_contract=contract, cells=cells, mentions=mentions, max_probes=4)
results = [await sentinels(action="execute_probe", probe=p, websearch_fn=websearch) for p in probes]
leads = await sentinels(action="find_novel_leads", probe_results=results, mentions=mentions)
for lead in leads:
    print(lead["policy"], lead["suggested_route"], lead["url"])
```
