---
name: instrumented-service-scaffold
description: Use when creating or reviewing a new long-running service so observability, runtime flags, tests, coverage gates, and release controls exist from the first commit.
---

# Instrumented Service Scaffold

Use this workflow when authoring or reviewing a new service, worker, daemon,
API, or other long-running software component.

## Baseline

From the first commit, the service must include:

- OpenTelemetry traces, metrics, and structured logs wired through the
  environment's normal exporter.
- Runtime flags through OpenFeature or the repository's established equivalent,
  with a source of truth that can be reviewed and rolled back.
- A test harness with unit tests, contract tests where interfaces matter, and a
  coverage gate appropriate to the risk of the service.
- PR-only delivery with branch protection and required checks for tests,
  coverage, release policy, and build/deploy validation.
- Conventional release or changelog metadata when the repository uses automated
  versioning.

Do not defer these to a later hardening pass. If the service is too small to
justify them, document that decision and keep it short-lived or non-production.

## Self-Debug Loop

The owner of the service needs the ability to:

1. Observe its own traces, metrics, and structured logs.
2. Form a concrete hypothesis from that telemetry.
3. Change behavior through a reviewed runtime flag or a normal PR.
4. Verify the user/system artifact and the telemetry after the change.

The loop is incomplete if it depends on a human manually spelunking logs on the
owner's behalf.

## Conformance Check

Before claiming the service is ready, verify the real repository contains:

- OpenTelemetry initialization code and at least one meaningful span or metric
  around a business-critical flow.
- Runtime flag client initialization and a reviewed flag source.
- Coverage configuration enforced by CI.
- Required PR checks and release policy checks in the repository settings or
  infrastructure source of truth.
- Documentation that explains how to observe the service and change flags.

Record any exception as a blocker or an explicit temporary waiver with a removal
date. Do not call the service production-ready while the baseline is missing.
