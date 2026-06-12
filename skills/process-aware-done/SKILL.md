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
- State the anti-shortcut check: no copied answer, no data leakage, no
  overfit/lookahead, no hallucinated route or feature, no unverified send.
- Record gaps explicitly. If verification is queued or blocked, call it
  waiting or blocked, not done.

## Hard Stops

- A circular evaluation is not an independent win.
- A merged PR with a broken user flow is not done.
- A synthetic artifact without source-backed verification is a demo only.
