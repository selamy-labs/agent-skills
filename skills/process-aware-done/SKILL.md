---
name: process-aware-done
description: Use when reporting or reviewing done, verified, passed, sent, matched, green, or complete claims. Requires artifact evidence plus a legitimate, reproducible path to the result.
---

# Process-Aware Done

Completion requires both:

1. **Artifact:** the real thing exists and works where it matters.
2. **Trajectory:** the path that produced it was legitimate, independent, and reproducible.

Do not accept proxies as completion. Passing CI is not the same as a working
product flow, a send ID is not the same as a received message, and a matched
answer is not trustworthy without source lineage.

## Checklist

- Name the artifact checked: PR, deployment, trace, email, document, workflow
  run, API response, or user-visible behavior.
- Explain how it was produced: source data, tool path, repo branch, execution
  path, or build/deploy path.
- For a bug, incident, or runtime edge case, name the same-PR regression
  ratchet that would have caught it. Use the cheapest durable layer:
  container-structure test or runtime smoke assertion for container/runtime
  shape, unit/regression test for code behavior, policy/Helm/IaC test for
  configuration gaps, or monitoring alert when no test can catch the failure.
- State the anti-shortcut check: no copied answer, no data leakage, no
  overfit/lookahead, no hallucinated route or feature, no unverified send.
- Record gaps explicitly. If verification is queued or blocked, call it
  waiting or blocked, not done.

## Hard Stops

- A circular evaluation is not an independent win.
- A merged PR with a broken user flow is not done.
- A synthetic artifact without source-backed verification is a demo only.
- A fix PR for a reproduced failure without its regression ratchet is
  incomplete.
