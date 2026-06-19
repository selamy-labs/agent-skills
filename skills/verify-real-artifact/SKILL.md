---
name: verify-real-artifact
description: Use when validating whether a send, deploy, workflow, document, trace, or product flow really landed or works. Focuses verification on the real artifact instead of logs or indirect proxies.
---

# Verify The Real Artifact

Use the closest observable artifact to the user or system outcome.

## Examples

- Email: fetch the sent or received message and verify recipient, subject,
  MIME shape, body content, and attachments or rendering.
- Web product: exercise the real flow in the target environment or read the
  trace/log for the specific request.
- Infrastructure: inspect the declared resource, rendered manifest, apply
  result, rollout, or synced application, not just the plan.
- Data/report: verify source lineage and compare against independently fetched
  inputs.

## Live State Checks

- Run presence and health checks with the same auth, environment, exec plugins,
  and context that the real operator or automation uses.
- Do not hide stderr on existence or health probes. Suppressed auth, plugin, or
  permission errors often masquerade as empty output.
- Corroborate important infrastructure state from at least two views when
  possible, such as declared config plus provider state, cluster API plus GitOps
  status, or user-visible behavior plus service traces.
- Treat a partial observer's empty result as unknown, not proof of absence.

## Report Shape

Use this compact form:

`Artifact verified: <what>. Path: <how produced>. Check: <how verified>. Gaps: <none or explicit>.`

If the best available evidence is only a proxy, say so and keep the task open
or blocked.
