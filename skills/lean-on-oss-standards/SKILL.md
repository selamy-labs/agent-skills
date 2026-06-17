---
name: lean-on-oss-standards
description: Use when choosing implementation patterns for observability, runtime configuration, API typing, CI, auth, or infrastructure. Biases toward proven open standards before custom machinery.
---

# Lean On OSS Standards

Before building custom machinery, reach for the battle-tested open standard that
already solves the problem. Most cross-cutting concerns — how a service emits
telemetry, flips a flag at runtime, types an API, gates CI — have a standard the
ecosystem has already hardened. Adopt it; spend your invention budget on the
domain logic that is actually yours.

## The test

For any infrastructure-shaped decision, ask: *is there a proven open standard for
this exact concern?*

- **Yes** → adopt it, even if it is slightly heavier than a hand-rolled version
  today. You inherit its tooling, docs, integrations, and bug fixes.
- **No, but the repo already has a strong local pattern** → follow the local
  pattern; consistency beats a second standard.
- **Genuinely no standard** → only then build, and keep the surface small so a
  standard can replace it later.

## Examples of the standards to reach for

These are illustrations of the discipline, not the point:

- Observability: OpenTelemetry traces, metrics, and structured logs.
- Runtime knobs: OpenFeature or the repository's existing flag client.
- API typing: shared types and typed RPC clients.
- Infrastructure: declarative IaC, package-native charts, GitOps, managed secret
  stores, keyless workload identity.
- CI/test policy: ecosystem-native test tools plus required status checks.

## Anti-pattern: bespoke reinvention

The smell is a homegrown component that re-implements what a standard already
does — a custom metrics wire format instead of OTel, a hand-rolled flag service
instead of OpenFeature, a one-off secret loader instead of a managed store. It
costs nothing the day you write it and everything later: no ecosystem, no
integrations, and a maintenance burden only you carry. If you find yourself
designing protocol-level plumbing for a solved concern, stop and adopt the
standard.
