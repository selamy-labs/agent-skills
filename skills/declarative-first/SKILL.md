---
name: declarative-first
description: Use for any change to a system's behavior, anywhere. Default to declaring desired state in version-controlled source that tooling reconciles, not making a live mutation. The test for any change is "where is the declared source, and what reconciles it?"
---

# declarative-first

The default for changing anything is declarative: the desired state lives in version-controlled source, and tooling reconciles reality to it. Not a live edit, not a one-off command whose effect exists only in a running system. This is a universal bias, not an infrastructure-only rule.

## The test for any change

Before making a change, answer two questions. If you cannot, you are about to create a mutation that nobody can reproduce or review:

1. **Where is the declared source?** The change is a diff to a file under version control.
2. **What reconciles it?** A named tool or pipeline applies the source to reality (and re-applying from a clean checkout reproduces the result).

A change that passes both is auditable, reviewable, revertable, and reproducible. A change that fails either exists **only as a live mutation**, the thing to avoid.

## What "declared" means by domain

The principle is one; it shows up everywhere:

- **Infrastructure and runtime.** Resources, cluster state, repo settings, secrets, and cron are declared and reconciled by IaC and GitOps, not edited live. (See `iac-not-ad-hoc`, `agents-as-iac-modules`.)
- **A system changing its own behavior.** When an automated system modifies itself, it ships a **source change that rebuilds and redeploys** it, never a hand-edit to a live, mutable volume. The new behavior is reproducible from the repo, not stranded in a pod that a restart wipes. (See `restart-resilience`.)
- **Configuration and feature state.** Flags and tunable knobs live in a declared flag source read at runtime, not set by hand on a live instance. Boot-time config is declared too.
- **Reporting and observability.** Telemetry pipelines and dashboards are declared (pipeline-as-code), and APIs are schema-first, so what is measured and exposed is reproducible, not clicked together in a console.

## Consequences that follow

- **One re-apply reproduces it.** Recovery is re-applying the source to a clean substrate, not rebuilding from memory. If recovery needs a human to remember steps, it was not declarative.
- **Supersede by changing the source, then deleting what it replaced** (see `adopt-and-delete`): no orphaned live state left behind.
- **Nothing exists only in a running system.** A live mutation is a defect to convert into declared source, or to justify explicitly as a temporary break with a follow-up to declare it.

## DONE means

The change landed as a diff to version-controlled source that a named tool reconciles; a re-apply from a clean checkout reproduces it; no behavior exists only as a live edit; and anything it supersedes was removed at the source.

Sources: declarative-configuration and GitOps practice; reproducible-systems and infrastructure-as-code principles; schema-first API and pipeline-as-code conventions.
