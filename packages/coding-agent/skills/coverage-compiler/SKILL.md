---
name: coverage-compiler
description: Compile a validated Query Contract into the mandatory coverage cells (source x region x language x query-family x entity-route) that must be searched before a run can claim provisional saturation, per SPEC.md's stopping criteria.
---

# Coverage Compiler

Takes the output of the query-contract skill and expands it into the mandatory
coverage cells defined in SPEC.md's two-queue architecture -- the non-negotiable
floor of work that must be attempted, separate from the adaptive frontier that
follows new leads later.

## How to use this

1. First get a validated query contract from the query-contract skill.
2. Pass it to `coverage_compiler(query_contract=...)`. It returns a list of
   coverage cell dicts, each with: source, region, language, query_family,
   entity_route, as_of, and status (always starts "pending").
3. This is a pure cross-product generator, not a search planner -- it does not
   rank, order, or execute cells. A later layer (not built yet) tracks status
   as workers complete each cell.

Important: when scope.geography in the query contract is empty (the query does
not restrict search geography), this skill defaults to a broad region/language
floor (US/EN, EU/EN, JP/JA, CN/ZH) rather than narrowing to any single region.
This matches the project's recall-first design rule: retrieve broadly, exclude
at admission, never at retrieval. A query like "regardless of asset owner
domicile" is exactly the case this guards against -- do not assume US-only
sourcing just because a criterion (e.g. "US FDA IND") mentions a US regulator.

## Usage

```python
contract = await query_contract(
    raw_query="Human PARP1-selective small molecule inhibitors with a US IND",
    scope={"target_or_mechanism": ["PARP1"], "as_of_date": "2026-09-29"},
    evidence={"required_output_fields": ["Drug Name", "Sponsor", "Development Stage"]},
)
cells = await coverage_compiler(query_contract=contract)
print(f"{len(cells)} mandatory coverage cells")
```
