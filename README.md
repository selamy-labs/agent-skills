# Agent Skills

Reusable, public `SKILL.md` workflows for AI coding and operations agents.

This repository is intentionally generic. It contains durable engineering
practices that can be used in any organization without depending on private
systems, customer names, internal paths, credentials, or product-specific
context.

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

## Public-Safety Rule

Everything in this repository must be safe for a public internet audience.
Do not add customer names, internal project names, private hostnames, local
filesystem paths, IP addresses, credentials, tokens, subscription or billing
details, or any organization-specific operating model.

CI enforces this with:

- `scripts/privacy_scan.py`: blocks known private names and secret/path
  patterns.
- `scripts/lint_skills.py`: validates every `SKILL.md` has required
  frontmatter and concise metadata.

## License And Attribution

This repository is MIT licensed. Some practices are inspired by common
industry patterns and public skill libraries. If a future skill adapts content
from another project, preserve that project's license and attribution in the
same change.
