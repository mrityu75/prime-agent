---
name: acquisition-worker
description: Execute one mandatory coverage cell (from coverage-compiler) via web search, returning raw structured findings tagged to that cell. First of several planned worker types -- this one covers general web/literature search only, reusing the existing websearch skill.
---

# Acquisition Worker (websearch-based)

Executes ONE mandatory coverage cell by building a search query from it and the
originating query contract, then calling the existing `websearch` skill. Returns
raw findings tagged with the cell they came from.

This worker does not judge relevance or admit anything -- per SPEC.md's Agent
execution boundary, workers commit structured deltas, never authoritative prose
lists or admission decisions. Parsing findings into structured records and
deciding what gets admitted are separate, later steps (evidence-state layer,
not built yet).

This is the first of several planned worker types (see SPEC.md Acquisition
layer: patents, literature, trial registries, company filings, regulatory
filings, conferences). It intentionally covers only general web search for
now -- prove the pipeline connects end-to-end before building out the rest
of the taxonomy.

## How to use this

1. Get coverage cells from the coverage-compiler skill.
2. For each cell, call `acquisition_worker(cell=..., query_contract=..., websearch_fn=websearch)`.
   You must pass the kernel's `websearch` global explicitly as `websearch_fn` --
   this keeps the worker testable in isolation outside the kernel.
3. Each call returns one result dict: cell, query (the search string built),
   findings_text (raw text from websearch), status ("completed" or "failed"),
   and error (None or a message). A failure here means the search itself
   failed -- log it as a failed lane per SPEC.md's design rules; never treat
   it as "no results exist".

   Note: the websearch skill reports most failures as returned text, not as
   exceptions (e.g. "Web search is not set up yet..." for a missing API key,
   or "Error searching for '...'" for a failed HTTP request). acquisition-worker
   checks the returned text against known failure signal strings
   (`WEBSEARCH_FAILURE_SIGNALS`) and converts a match to status "failed", with
   the text moved to `error` and `findings_text` set to None, instead of
   trusting it as real findings. Add new signals to that list as they appear.

4. Run cells in parallel via subagents for real throughput -- this skill
   itself does not parallelize; that's the caller's job (see SPEC.md's
   two-queue frontier and design rule #2: independent workers, not shared
   blind spots).

## Usage

```python
contract = await query_contract(
    raw_query="Human PARP1-selective small molecule inhibitors with a US IND",
    scope={"target_or_mechanism": ["PARP1"], "modality": ["small molecule"], "as_of_date": "2026-09-29"},
    evidence={"required_output_fields": ["Drug Name", "Sponsor", "Development Stage"]},
)
cells = await coverage_compiler(query_contract=contract)

result = await acquisition_worker(
    cell=cells[0],
    query_contract=contract,
    websearch_fn=websearch,
)
print(result["status"], result["query"])
print(result["findings_text"][:500])
```
