---
name: verdict-queue
description: Run admission verdicts for the drug-sourcing pipeline as bounded, resumable units (one direct model call per candidate or small batch), recording per-candidate outcomes including provider content-filter blocks, and return verbatim-quote claims for admission-gate.
---

# Verdict Queue

The standing design for the verdict step (SPEC.md Screening & admission).
Verdicts are made by direct model calls driven by the orchestrator, one
candidate (or a small batch) per call. They are not made inside a long agent
turn: agent turns stalled on large candidate sets and on provider
content-filter blocks during the 10x pilot.

- The verdict model sees numbered evidence quotes and cites one by index. The
  module copies the exact quote, URL, and mention id into each claim, so claim
  quotes are verbatim by construction.
- Each candidate keeps an outcome: `judged`, `content_filtered` (the provider's
  safety filter returned no output), `parse_failed`, or `error`.
  `content_filtered` items are retried on the same model (the filter is not
  deterministic) and otherwise stay flagged. They are never sent to a fallback
  model: verdicts stay with one model to protect admission precision.
- The queue is a JSONL file saved after every unit, so `process` can be
  re-run to resume, and `max_items` bounds the work per call.

## Usage

```python
q = await verdict_queue(action="build_queue", path="~/runs/nad/queue.jsonl",
                        packets=extraction["packets"], order=ranked_names)
spec = {"raw_query": contract["raw_query"], "criteria": criteria,
        "required_fields": contract["evidence"]["required_output_fields"],
        "exclusions": contract["scope"]["exclusions"],
        "inference_rules": contract["semantics"]["inference_rules"]}
print(await verdict_queue(action="process", path=q["path"], spec=spec, concurrency=4))
claims = await verdict_queue(action="claims", path=q["path"])
```

Requires AWS_BEARER_TOKEN_BEDROCK and AWS_REGION for the default Bedrock
caller. `converse_fn` can be injected for tests.
