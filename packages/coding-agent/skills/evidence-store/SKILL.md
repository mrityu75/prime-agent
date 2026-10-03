---
name: evidence-store
description: Persist pipeline output (runs, coverage cells, acquisition-worker raw mentions) to Supabase via its REST API. Requires SUPABASE_URL and SUPABASE_SECRET_KEY in the environment, and the runs/coverage_cells/raw_mentions tables to already exist (see SPEC.md for schema).
---

# Evidence Store

Persists the pipeline's output to Supabase so it survives past one session,
and so recall can eventually be measured against test_cases/ gold data. This
skill does no judgment -- it only stores what it's given. Admission, claim
extraction, and identity/lineage resolution are separate, later layers (not
built yet -- see SPEC.md's Evidence state / admission gate items).

## Setup

Requires SUPABASE_URL and SUPABASE_SECRET_KEY as environment variables (both
already configured in this project's .env). Requires the runs, coverage_cells,
and raw_mentions tables to already exist in the Supabase project -- this skill
does not create them. If a call fails with a connection or table-not-found
error, check those two things before assuming the skill itself is broken.

## How to use this

Call `evidence_store(action=..., **kwargs)` with one of three actions, always
in this order for a given pipeline run:

1. `action="create_run"` with `raw_query` and `query_contract` -- creates one
   row in `runs`, returns it (use its `id` for the next step).
2. `action="store_coverage_cells"` with `run_id` (from step 1) and `cells`
   (coverage-compiler's output list) -- inserts one row per cell, returns
   the inserted rows with their generated ids, in the same order as input.
3. `action="store_raw_mention"` with `coverage_cell_id` (from step 2, pick
   the matching cell's id) and `worker_result` (one acquisition-worker
   result dict) -- inserts one raw_mentions row, returns it.

All three raise RuntimeError if the insert fails or if SUPABASE_URL /
SUPABASE_SECRET_KEY aren't set. `action="bogus"` or any unrecognized action
raises ValueError listing the valid actions.

## Usage

```python
contract = await query_contract(
    raw_query="Human PARP1-selective small molecule inhibitors with a US IND",
    scope={"target_or_mechanism": ["PARP1"], "modality": ["small molecule"], "as_of_date": "2026-09-29"},
    evidence={"required_output_fields": ["Drug Name", "Sponsor", "Development Stage"]},
)
cells = await coverage_compiler(query_contract=contract)

run_row = await evidence_store(action="create_run", raw_query=contract["raw_query"], query_contract=contract)
stored_cells = await evidence_store(action="store_coverage_cells", run_id=run_row["id"], cells=cells)

result = await acquisition_worker(cell=cells[0], query_contract=contract, websearch_fn=websearch)
mention = await evidence_store(action="store_raw_mention", coverage_cell_id=stored_cells[0]["id"], worker_result=result)
print(mention["id"])
```
