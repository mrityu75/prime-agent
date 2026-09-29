---
name: query-contract
description: Parse a natural-language drug-sourcing request into a structured Query Contract (Scope, Semantics, Evidence, Access) per SPEC.md's Input Contract table, and validate it before passing it downstream.
---

# Query Contract

Turns a raw natural-language sourcing request into the structured Query Contract
defined in the project's SPEC.md (Scope, Semantics, Evidence, Access). This is the
first stage of the pipeline — every downstream layer (coverage compiler, admission
gate, eval) consumes this contract's output.

## How to use this

1. Read the raw client query (e.g. from test_cases/.../query.txt).
2. Extract the four sections yourself, using your own reasoning — this skill does
   NOT do the extraction for you, it only validates and structures what you give it:
   - **scope**: target_or_mechanism (list), modality (list), indication (list),
     stage (list), geography (list), sponsor_constraints (text), exclusions (list),
     as_of_date (YYYY-MM-DD string)
   - **semantics**: counting_unit ("asset", "program", or "patent_family"),
     inference_rules (text), ambiguity_policy ("include_and_flag" or "exclude",
     defaults to "include_and_flag" per the project's recall-first design rule),
     definitions (dict of term -> definition)
   - **evidence**: required_output_fields (list), acceptable_source_classes (list),
     full_document_required (bool, defaults True), source_cutoff (YYYY-MM-DD or None)
   - **access**: public_sources (list), licensed_sources (list),
     subscriber_sources (list), blocked_sources (list)
3. Call `await query_contract(...)` with your extracted values. It validates
   required fields, applies defaults, and returns a JSON-serializable dict.
4. If validation fails, it raises ValueError listing every missing/invalid field.
   Fix your extraction and retry. Never invent a value the query doesn't support —
   leave a field empty/None if the query genuinely doesn't specify it.

## Usage

Call the prepared `query_contract` import directly in the Python kernel:

```python
contract = await query_contract(
    raw_query="PARP1-selective inhibitors with a US IND",
    scope={
        "target_or_mechanism": ["PARP1"],
        "modality": ["small molecule"],
        "as_of_date": "2026-09-29",
    },
    semantics={
        "counting_unit": "asset",
    },
    evidence={
        "required_output_fields": ["Drug Name", "Sponsor", "Development Stage"],
        "acceptable_source_classes": ["patents", "trials registries", "company filings"],
    },
    access={
        "public_sources": ["PubMed", "ClinicalTrials.gov", "USPTO"],
    },
)
print(contract)
```
