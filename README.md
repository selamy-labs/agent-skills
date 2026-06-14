# Agent Skills

Reusable, public `SKILL.md` workflows for AI coding and operations agents.

This repository is intentionally generic. It contains durable engineering
practices that can be used in any organization without depending on customer
names, local filesystem paths, credentials, or product-specific
context.

Third-party public skills may be consumed from their upstream projects with
their own license terms; they should not be republished in this repository.

## Skills

- `process-aware-done`: require artifact verification and process evidence
  before accepting completion claims.
- `verify-real-artifact`: verify the real user/system artifact instead of a
  proxy such as logs or CI alone.
- `grounded-generation`: generate from verified inputs with explicit source
  lineage.
- `lean-on-oss-standards`: choose idiomatic open standards before custom
  machinery.
- `iac-not-ad-hoc`: keep infrastructure and runtime configuration
  reproducible from source.
- `data-connector-building`: build source-backed, observable, fixture-tested
  data connectors.
- `instrumented-service-scaffold`: create long-running services with
  observability, runtime flags, tests, coverage gates, and release controls
  from the first commit.
- `reddit-research`: collect Reddit discussion signals with source lineage,
  rate-limit discipline, and terms-aware access choices.
- `yield-on-wait`: checkpoint long waits, switch to other queued work, and
  resume from durable state instead of watching spinners.
- `early-return-over-else`: reduce nesting with guard clauses and tests.
- `map-dispatch-over-conditionals`: replace stable conditional chains with
  explicit dispatch tables.
- `enums-codify-behavior`: make behavior modes explicit and exhaustively
  handled.
- `complexity-budgets`: enforce branching, nesting, and function-size limits
  with standard linters.
- `feature-coverage-not-just-line-coverage`: pair coverage percentages with
  named behavior checks and anti-overfit evidence.
- `authoring-html-email`: compose and verify client-safe HTML email with
  correct multipart MIME and render checks.

## Decision Docs

- `docs/code-style-foundation.md`: language-agnostic code-style baseline and
  current open-source toolchain bindings.

## Versioning

Consumers should pin this repository by semantic version tag, not by a raw
commit SHA. The first release is `v0.1.0`; later releases follow
conventional-commit semantics:

- `feat:` creates a minor release.
- `fix:`, `perf:`, and `refactor:` create a patch release.
- `!` in the commit header or `BREAKING CHANGE` in the body creates a major
  release.

The release workflow validates all skills, creates the next `vX.Y.Z` tag, and
publishes a GitHub release from `main`. IaC consumers should upgrade by changing
the pinned tag in a reviewable PR.

## Public API Stability

Treat each skill directory name and frontmatter `name` as a public API.
Additions are cheap; removals and renames are disruptive for consumers that pin
and invoke skills by name.

- Incubate new workflows outside this public repository until the name,
  frontmatter, structure, and behavior are stable.
- Prefer adding a sharper new skill over publishing a thin slogan.
- Mark a skill deprecated in one minor release before removing or renaming it.
  The deprecation note must point to the replacement or explain that there is no
  replacement.
- Remove or rename a public skill only in a major release. The breaking commit
  must use `!` in the conventional-commit header or include `BREAKING CHANGE`
  in the body.
- Never silently remove, rename, or repurpose an existing skill.

CI enforces the major-release part of this policy with
`scripts/public_api_stability.py`.

## Public-Safety Rule

Everything in this repository must be safe for a public internet audience.
Do not add customer names, product-specific project names, non-public
hostnames, local filesystem paths, IP addresses, credentials, tokens,
subscription or billing details, or any organization-specific operating model.

The scanner blocks public-safety leaks, including:

- organization-specific product, agent, vendor, customer, and person names
- non-public process/tool terms and operational file paths
- localhost identities and non-public service hostnames
- common credential, token, private-key, secret-manager, and `pass` patterns

CI enforces this with:

- `scripts/privacy_scan.py`: blocks known private names and secret/path
  patterns.
- `scripts/test_privacy_scan.py`: proves representative private-boundary leaks
  fail the scanner.
- `scripts/public_api_stability.py`: blocks removals or renames unless the
  release is explicitly marked breaking.
- `scripts/lint_skills.py`: validates every `SKILL.md` has required
  frontmatter and concise metadata.

## License And Attribution

This repository is MIT licensed. Some practices are inspired by common
industry patterns and public skill libraries. If a future skill adapts content
from another project, preserve that project's license and attribution in the
same change.
