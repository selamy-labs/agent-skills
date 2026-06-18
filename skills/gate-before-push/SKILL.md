---
name: gate-before-push
description: Use before opening a pull request. Reproduce the repository's OWN CI gates locally and make them green first — including its bespoke checks — so the PR arrives green and merges first-try instead of burning scarce CI iterations.
---

# gate-before-push

Open a PR only after it would already go green. Every red CI iteration costs a full runner cycle plus review latency; when runner capacity is constrained, a push-and-pray PR steals capacity from everyone. The fix is to run the **repository's own gates** locally before the push — not "I ran some tests," but the exact checks CI will run.

## Read the gate list from CI — don't guess

The trap is running the obvious checks (build, unit tests) and missing the **bespoke** ones that only this repo enforces — the gates you'd otherwise discover failing only after a runner picks the PR up.

1. **Open the CI definition** (the workflow / pipeline config) and enumerate every gate step in order: lint, format, type-check, tests + coverage threshold, and the project-specific scanners — secret/privacy scanners, dependency/denylist drift, license checks, API-stability checks, and PR-title/commit-message validators for squash-release repos.
2. **Run each step's exact command locally**, in the same order. Use the repo's own scripts (the same entrypoints CI calls), not a paraphrase — a paraphrase drifts from what CI actually runs.
3. **Fix locally until all are green.** A bespoke scanner often false-positives on innocent text (e.g. a phrase that resembles a secret pattern); reword and re-run rather than pushing and hoping.
4. **Match the metadata gates too** — if CI validates the PR title or commit format (conventional commits, squash-release prefixes), confirm yours conforms before opening the PR.

## Why bespoke-first

Generic checks rarely fail twice; the **repo-specific** scanners and validators are where push-and-pray loses. Those are exactly the cheap-to-run, expensive-to-discover-remotely gates — running them locally is the highest-leverage minute you spend.

## Pairs with

- CI economy: a locally-green PR is the per-PR analogue of keeping a stack short and its upper members drafted — don't multiply runner load with avoidable red runs.
- Green locally is **not** done: done is merged + the artifact verified on the default branch, not a passing local run.

## DONE means

Before the PR opened, every gate the CI will run was run locally from the repo's own entrypoints and passed (including the bespoke scanners and the title/commit validators); the PR's **first** CI run is green; no red-iterate-push cycles were spent on issues that were findable locally.

Sources: shift-left / fail-fast testing practice; trunk-based development pre-merge checks; conventional-commit release gating.
