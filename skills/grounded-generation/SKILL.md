---
name: grounded-generation
description: Use when generating content, reports, summaries, evaluations, or code against external facts. Requires verified source data, explicit lineage, and no invented capabilities or copied answers.
---

# Grounded Generation

Generate only from verified inputs. Missing data is a task, not permission to
invent.

## Tag every datum's lineage

Mark each value the output relies on as one of:

- **primary** — read directly from the system of record this run.
- **copied** — taken from another candidate, prior output, or cache; carries
  that source's correctness, not independent confirmation.
- **synthetic** — fixture, sample, or placeholder; never a result.
- **derived** — computed from the above; inherits the weakest tag it draws on.

Lineage is the behavior this skill adds: an output you cannot tag is an output
you cannot ground. Tags propagate — a derived claim is only as primary as its
weakest input.

## Apply per generation type

- For comparisons, prove each candidate is **primary**-sourced from the same
  underlying data. If one answer is **copied** from the control, the result
  validates formatting only.
- For code, build against routes, schemas, functions, and APIs that exist.
  Nonexistent endpoints and fabricated helpers must fail review.
- For analysis and forecasts, run leakage, lookahead, and overfit checks before
  trusting any performance claim.
