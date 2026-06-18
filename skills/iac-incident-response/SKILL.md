---
name: iac-incident-response
description: Use during infrastructure outages when fixes must preserve declarative source of truth instead of leaving live manual drift behind.
---

# IaC Incident Response

Use this when infrastructure is broken and there is pressure to fix it directly.
The goal is fast recovery without creating hidden state that the next apply will
destroy or fail to reproduce.

## Rules

- Prefer a source change, merge, and apply over direct mutation.
- If an emergency manual resource exists, import it into state or replace it
  through a declared resource before declaring recovery complete.
- Do not bridge a branch by hand-applying manifests that the main branch does
  not own.
- Escalate the precise blocker rather than bypassing the source of truth.

## Incident Workflow

1. Identify the failed declarative boundary:
   - infrastructure plan/apply
   - GitOps sync
   - controller reconciliation
   - CI secret/config propagation
2. Make the smallest source change that lets the normal reconciler fix the
   system.
3. If live state already changed, reconcile it:
   - import hand-made resources into state, or
   - remove them only after the declared replacement is ready.
4. Run a no-drift check after recovery:
   - plan shows no unexpected changes, or expected changes are reviewed
   - GitOps reports synced/healthy
   - a real workload or workflow proves the path works
5. Capture the guard that would have caught the failure earlier.

## Acceptable Diagnostics

Read-only cloud, cluster, and CI commands are fine. They become unsafe when they
turn into live patching, deleting, scaling, or secret mutation without a
matching source change.

## Done

- A fresh apply or sync can reproduce the recovered state.
- Manual emergency resources are gone or imported.
- The incident report names any remaining external blocker.
