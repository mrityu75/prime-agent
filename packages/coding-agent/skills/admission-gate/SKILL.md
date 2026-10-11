---
name: admission-gate
description: Deterministic admission layer for the drug-sourcing pipeline. Reads a run's coverage cells and raw mentions from evidence-store's Supabase tables, checks that each agent-extracted claim is backed by stored, citable text, and sorts every candidate into admitted, review, candidates, or rejected, with a coverage report, per SPEC.md's Screening & admission layer.
---

# Admission Gate

The judgment layer (SPEC.md Layers table, "Screening & admission"): a separate
admission service evaluates each atomic criterion and renders the final table
deterministically from supported claims.

This skill makes no live model judgment (CLAUDE.md: "Admission decisions are
deterministic from stored evidence"). You, the calling agent, read the raw
mentions (and any full documents you fetch) and extract claims. The skill
checks each claim against the stored text and applies fixed rules. Same input,
same output.

## Data it reads

`action="load_run"` reads evidence-store's live tables via the Supabase REST
API (needs SUPABASE_URL and SUPABASE_SECRET_KEY, same as evidence-store):

- `runs`: id, raw_query, query_contract, created_at
- `coverage_cells`: id, run_id, source, region, language, query_family,
  entity_route, as_of, status, note, created_at
- `raw_mentions`: id, coverage_cell_id, query_used, findings_text, status,
  error, created_at

It writes nothing. There are no claims or admissions tables yet.

## Claims you supply

One dict per (candidate, criterion) observation:

| Key | Content |
|---|---|
| `candidate` | Name or code exactly as the source writes it |
| `criterion` | A criterion name (see below), an exclusion string, or a required output field |
| `verdict` | `"met"`, `"not_met"`, or `"unclear"` |
| `quote` | Verbatim text from the source that supports the verdict |
| `source_url` | The URL the quote comes from |
| `mention_id` | The raw_mentions id the quote is in (for snippet evidence) |
| `value` | The field value, for required output fields (e.g. Sponsor) |
| `basis` | `"stated"` (default): the quote states the fact. `"inferred"`: the quote states a premise of a declared inference rule |
| `inference_rule` | For `basis="inferred"`: the `id` of a rule in the contract's `semantics.inference_rules` |
| `party` | For sponsor/owner-domicile criteria: `"originator"`, `"current_owner"`, `"originator_and_current_owner"`, `"licensee"`, or `"sponsor"` |

Criteria default to the non-empty scope fields of the query contract
(target_or_mechanism, modality, indication, stage, geography,
sponsor_constraints). Pass `criteria=[...]` to override. Exclusions come from
scope.exclusions, output fields from evidence.required_output_fields.

## Rules

- **Support check.** A claim counts only if its quote appears in the cited
  mention's findings_text (case and whitespace ignored) and its source_url
  appears in that mention, or the quote appears in `documents[source_url]`
  (full text you read). Other claims go to `unsupported_claims` with the
  problem.
- **Snippets are leads.** If evidence.full_document_required is true (the
  default), snippet-only support for any criterion or field sends the
  candidate to review, never to admission.
- **Absence is not rejection.** Only a supported "not_met", or a supported
  "met" on an exclusion, rejects. ambiguity_policy "exclude" also rejects
  "unclear"; "include_and_flag" (default) sends it to review.
- **Buckets.** `admitted`: every criterion and field met with full-document
  support, no conflicts. `review`: at most one criterion/field missing, or
  contradictions, conflicting field values, unclear, or snippet-only support.
  `candidates`: two or more missing (kept as leads). `rejected`: see above.
  Every candidate appears in exactly one bucket, with reasons and citations.
- **Sponsor domicile is judged over the whole ownership history.** For
  `sponsor_constraints` (or any criterion whose claims carry `party`), the
  candidate is rejected only when every claim is "not_met" and the claims
  cover both the originator and the current owner (or one
  `originator_and_current_owner` claim). A mixed history (e.g. a US
  originator acquired by a Swiss company), an unclear claim, or a not_met for
  only one party goes to review with the per-party verdicts in
  `criteria[...]["parties"]`.
- **Inference is explicit and tagged.** An inferred claim counts only if its
  rule is declared in `semantics.inference_rules`, the rule's criterion is
  the claim's criterion, and the verdict is "met" (inference never rejects).
  Its quote must pass the same support check. A criterion met by a stated
  claim has `verdict_basis` "stated"; one met only through a rule has
  "inferred" plus the rule ids. A row's `verdict_basis` is "inferred" if any
  criterion is inferred (listed in `inferred_criteria`), else "stated".
  Admitted rows of both kinds stay in `admitted`, and `admitted_by_basis`
  counts them, so direct evidence and regulatory inference are never mixed
  silently. A stated "not_met" against an inferred "met" is a contradiction
  and goes to review.
- **No merges.** Names that differ only in punctuation or case (AZD5305 /
  AZD-5305), or labels that share a part ("saruparib (AZD5305)" / "AZD5305";
  labels are split on parentheses, commas, semicolons, and slashes) are
  listed in `possible_same_as`. They are not merged.
- **Never "complete".** `coverage_report.status` is "incomplete" or
  "blocked" (all lanes failed). Stopping checks 1 and 5 are computed; 2-4 are
  "not_evaluated" because they need identity resolution, sentinel sweeps, and
  an audit.

## Usage

```python
data = await admission_gate(action="load_run", run_id=run_row["id"])
# Read data["mentions"] (and fetched full documents), then extract claims:
claims = [
    {"candidate": "RP12146", "criterion": "target_or_mechanism", "verdict": "met",
     "quote": "selective, and potent small molecule", "source_url": "https://...",
     "mention_id": data["mentions"][0]["id"]},
]
result = await admission_gate(
    action="evaluate",
    query_contract=data["run"]["query_contract"],
    claims=claims,
    mentions=data["mentions"],
    cells=data["cells"],
    documents={},  # {source_url: full text} for documents you read
)
print([r["candidate"] for r in result["admitted"]], result["coverage_report"]["status"])
```
