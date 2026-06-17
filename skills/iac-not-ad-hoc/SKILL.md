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

Manual commands are diagnostics or emergency unblocks only. If used, codify the
equivalent declarative change in the same lane and record why the manual step
was necessary.

## Module Structure (Terraform / OpenTofu)

Keep each module to four files: `main.tf`, `variables.tf`, `outputs.tf`, and
`locals.tf`. The root module additionally has `providers.tf` and, when remote
state is used, `backend.tf`. If you find you need another file, that is usually
a signal to decompose into a smaller submodule rather than to add a file.

Share reusable modules through a dedicated modules repository and import them by
pinned git source:

    module "example" {
      source = "git::https://github.com/<org>/terraform-modules.git//modules/<name>?ref=<tag>"
    }
