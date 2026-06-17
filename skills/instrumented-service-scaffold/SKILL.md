---
name: instrumented-service-scaffold
description: Use when creating or reviewing a new long-running service so observability, runtime flags, tests, coverage gates, and release controls exist from the first commit.
---

# Instrumented Service Scaffold

Use this workflow when authoring or reviewing a new service, worker, daemon,
API, or other long-running software component.

## Baseline

From the first commit, the service must include:

- OpenTelemetry traces, metrics, and structured logs wired through a real
  exporter.
- A runtime flag client (e.g. OpenFeature) backed by a flag source that can be
  reviewed and rolled back without a redeploy.
- A formatter, linter, and type/static-analysis gate using the language's
  standard tools.
- Complexity and nesting budgets that stop new outliers before they turn into
  review-time style debates.
- A test harness: unit tests, contract tests where interfaces matter, at least
  one feature check for promised behavior, and a coverage gate sized to the
  service's risk.
- PR-only delivery with branch protection and required checks for tests,
  coverage, release policy, and build/deploy validation.
- State-changing delivery workflows (deploy, apply, image-publish) that
  serialize per target via a concurrency group with `cancel-in-progress: false`;
  read-only CI may cancel superseded runs.
- Conventional release or changelog metadata when the repo uses automated
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

Before claiming the service is ready, grep the real repository for each item —
not the intent, the artifact:

- OpenTelemetry init code and at least one meaningful span or metric on a
  business-critical flow.
- Runtime flag client init pointing at a reviewed flag source.
- Formatter, linter, static-analysis, complexity, and coverage config enforced
  by CI (the config files plus the CI job that runs them).
- At least one feature-level behavior check for the primary promise, not only
  line coverage.
- State-changing workflows (deploy, apply, release, image-publish) that declare
  queue-not-cancel concurrency scoped to serialize each target environment,
  state file, or published artifact.
- Required PR checks and release-policy checks present in the repo settings or
  IaC source of truth.
- Docs that explain how to observe the service and change a flag.

Record any gap as a blocker or an explicit temporary waiver with a removal date.
Do not call the service production-ready while the baseline is missing.
