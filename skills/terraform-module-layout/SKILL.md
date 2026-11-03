---
name: terraform-module-layout
description: Use when creating, reviewing, or restructuring Terraform or OpenTofu modules. Enforces Google-recommended module file layout and cohesion-based module boundaries.
---

# Terraform / OpenTofu Module Layout

Modules follow Google's canonical file layout. The structure keeps modules navigable,
reviewable, and reusable; deviations are caught by automated lint.

## Core Required Files (every module)

| File | Purpose |
|---|---|
| `main.tf` | Primary resources, data sources, and module calls |
| `variables.tf` | Input variable declarations |
| `outputs.tf` | Output declarations |
| `versions.tf` | `required_providers` and `required_version` constraints |
| `README.md` | Module documentation: purpose, inputs, outputs, and usage |

## Logical Resource Files

In accordance with Google's Terraform style guide, modules may split complex resource
configurations into logically grouped `.tf` files named `[a-z0-9_]+\.tf`:

- `network.tf`, `firewall.tf`, `iam.tf`, `dns.tf`, `storage.tf`, `compute.tf`
- `locals.tf` for local value expressions
- `data.tf` for data source queries

## Root Module Only

| File | Purpose |
|---|---|
| `providers.tf` | Provider configuration blocks |
| `backend.tf` | Backend configuration |

Reusable child modules never configure providers or backends directly. They declare
provider requirements in `versions.tf` and receive provider instances from the caller.

## Prohibited in Reusable Modules

- Environment root directories (`envs/`, `environments/`)
- Variable definitions files (`*.tfvars`, `*.tfvars.json`)
- State files (`*.tfstate`, `*.tfstate.backup`)

## Companion Artifacts

Every module ships with:

- `README.md` documenting purpose, inputs, outputs, and usage.
- `examples/` with at least a minimal and a complete example (these double as
  test fixtures).
- `tests/` with `*.tftest.hcl` files exercising the module.

## Module Boundary Guardrail

Split into submodules based on subsystem cohesion (own lifecycle, independently
reusable, clean interface), never on line count alone. Module boundaries cost
plumbing, moved blocks, and state surgery. A cohesive 350-line module beats
three anemic submodules. The layout constraint creates natural pressure to
keep modules focused; the cohesion test decides when to cut.
