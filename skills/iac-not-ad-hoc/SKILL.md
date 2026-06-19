---
name: iac-not-ad-hoc
description: Use for infrastructure, repository settings, cloud resources, Kubernetes, runner, secret, cron, runtime config, or deployment changes. Requires a declarative source of truth.
---

# IaC, Not Ad Hoc

Infrastructure and runtime configuration must be reproducible from source.

## Required Path

- Repository settings: manage through an infrastructure provider where
  possible.
- Cloud resources and IAM: declare in infrastructure as code with keyless auth
  where possible.
- Kubernetes/runtime: use manifests, charts, and GitOps rather than live edits.
- Secrets: use the established secret manager and sync mechanism. Never put
  credentials in chat or repositories.
- Cron/config: commit it to the service or agent repository, not a live pod.

Manual mutation is not an implementation path. During incidents, use live
commands for read-only diagnosis, imports, plans, and verification; make the
state-changing fix through the declared source of truth. If a resource already
exists because someone created it by hand, import it into the declarative state
instead of leaving it unmanaged or destroying and recreating it for tidiness.

If the declarative route is blocked, escalate the specific blocker and keep the
system's status explicit. Do not bridge the gap with an untracked live edit.

## Incident Bar

- Merge GitOps or IaC changes; do not point live controllers at a temporary
  branch.
- Use secret-manager and ExternalSecret-style sync paths for secrets; do not
  create live Kubernetes secrets by hand.
- End-state test: a clean apply or sync from the declared source recreates the
  required stack without hidden manual steps.

## Module Structure (Terraform / OpenTofu)

For Terraform/OpenTofu module file layout, see the `terraform-module-layout`
skill.
