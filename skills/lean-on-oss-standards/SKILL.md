---
name: lean-on-oss-standards
description: Use when choosing implementation patterns for observability, runtime configuration, API typing, CI, auth, or infrastructure. Biases toward proven open standards before custom machinery.
---

# Lean On OSS Standards

Prefer idiomatic, ecosystem-standard tools unless the repository has a stronger
local pattern.

## Defaults

- Observability: OpenTelemetry traces, metrics, and structured logs.
- Runtime knobs: OpenFeature or the repository's established equivalent.
- TypeScript API safety: shared types and typed RPC clients.
- Infrastructure: declarative infrastructure as code, package-native charts,
  GitOps, managed secret stores, and keyless workload identity where possible.
- CI/test policy: repository-native test tools plus required status checks.

Choose the path of least resistance that preserves speed, structure,
reproducibility, testability, observability, and traceability.
