---
name: grounded-generation
description: Use when generating content, reports, summaries, evaluations, or code against external facts. Requires verified source data, explicit lineage, and no invented capabilities or copied answers.
---

# Grounded Generation

Generate only from verified inputs. Missing data is a task, not permission to
invent.

## Data Lineage Rules

- List each source used and whether it is primary, copied, synthetic, or
  derived.
- For comparisons, prove each candidate independently sourced the same
  underlying data. If one answer copied the control, the result validates
  formatting only.
- For code, build against routes, schemas, functions, and APIs that exist.
  Nonexistent endpoints and fabricated helpers must fail review.
- For analysis and forecasts, include leakage, lookahead, and overfit checks
  before trusting performance claims.

## Output Rule

Separate facts, inferences, and open questions. Do not blur them.
