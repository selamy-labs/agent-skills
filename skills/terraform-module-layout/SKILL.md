---
name: terraform-module-layout
description: Use when creating, reviewing, or restructuring Terraform or OpenTofu modules. Enforces a canonical file layout and cohesion-based module boundaries.
---

# Terraform / OpenTofu Module Layout

Every module uses a fixed set of files. The constraint keeps modules navigable
and reviewable; deviations are caught by automated lint.

## Required Files (every module)

| File | Purpose |
|---|---|
| `main.tf` | Resources and module calls |
| `variables.tf` | Input variable declarations |
| `outputs.tf` | Output declarations |
| `locals.tf` | Local value expressions |
| `versions.tf` | `required_providers` and `required_version` |

## Optional File

`data.tf` is the single allowed extra for data sources when separating them
improves readability. No other filenames are permitted.

## Root Module Only

| File | Purpose |
|---|---|
| `providers.tf` | Provider configuration blocks |
| `backend.tf` | Backend configuration |

Child modules never configure providers or backends. They declare provider
requirements in `versions.tf` and receive provider instances from the caller.

## Companion Artifacts

Every module ships with:

- `README.md` documenting purpose, inputs, outputs, and usage.
- `examples/` with at least a minimal and a complete example (these double as
  test fixtures).
- `tests/` with `*.tftest.hcl` files exercising the module.

## Module Boundary Guardrail

Split into submodules based on subsystem cohesion (own lifecycle, independently
reusable, clean interface), never on line count alone. Module boundaries cost
plumbing, moved blocks, and state surgery. A cohesive 350-line `main.tf` beats
three anemic submodules. The fixed-file constraint creates natural pressure to
keep modules focused; the cohesion test decides when to cut.
