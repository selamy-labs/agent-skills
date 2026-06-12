# Agent Skills

Reusable, public `SKILL.md` workflows for AI coding and operations agents.

This repository is intentionally generic. It contains durable engineering
practices that can be used in any organization without depending on private
systems, customer names, internal paths, credentials, or product-specific
context.

This public repository is only for original, scrubbed, generic skills. Private
deployment images may combine these public skills with private internal skills,
but private skills must stay in private sources and must not be copied here.
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

## Public-Safety Rule

Everything in this repository must be safe for a public internet audience.
Do not add customer names, internal project names, private hostnames, local
filesystem paths, IP addresses, credentials, tokens, subscription or billing
details, or any organization-specific operating model.

The scanner blocks public/private boundary leaks, including:

- private org, product, agent, vendor, customer, and person names
- internal process/tool terms and operational file paths
- localhost identities and internal service hostnames
- common credential, token, private-key, secret-manager, and `pass` patterns

CI enforces this with:

- `scripts/privacy_scan.py`: blocks known private names and secret/path
  patterns.
- `scripts/test_privacy_scan.py`: proves representative private-boundary leaks
  fail the scanner.
- `scripts/lint_skills.py`: validates every `SKILL.md` has required
  frontmatter and concise metadata.

## License And Attribution

This repository is MIT licensed. Some practices are inspired by common
industry patterns and public skill libraries. If a future skill adapts content
from another project, preserve that project's license and attribution in the
same change.
