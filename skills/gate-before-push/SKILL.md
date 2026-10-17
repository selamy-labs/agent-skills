---
name: gate-before-push
description: Use before opening a pull request. Reproduce the repository's OWN CI gates locally and make them green first — including its bespoke checks — so the PR arrives green and merges first-try instead of burning scarce CI iterations.
---

# gate-before-push

Open a PR only after it would already go green. Every red CI iteration costs a full runner cycle plus review latency; when runner capacity is constrained, a push-and-pray PR steals capacity from everyone. The fix is to run the **repository's own gates** locally before the push — not "I ran some tests," but the exact checks CI will run.

## Read the gate list from CI — don't guess

The trap is running the obvious checks (build, unit tests) and missing the **bespoke** ones that only this repo enforces — the gates you'd otherwise discover failing only after a runner picks the PR up.

1. **Open the CI definition** (the workflow / pipeline config) and enumerate every gate step in order: lint, format, type-check, tests + coverage threshold, and the project-specific scanners — secret/privacy scanners, dependency/denylist drift, license checks, API-stability checks, and PR-title/commit-message validators for squash-release repos.
2. **Run each locally reproducible step's exact command**, in the same order. Use the repo's own scripts (the same entrypoints CI calls), not a paraphrase. Identify platform-only checks explicitly, record the hosted run needed, and keep them pending until they actually pass; do not claim they were reproduced locally.
3. **Fix locally until all are green.** A bespoke scanner often false-positives on innocent text (e.g. a phrase that resembles a secret pattern); reword and re-run rather than pushing and hoping.
4. **Match the metadata gates too** — if CI validates the PR title or commit format (conventional commits, squash-release prefixes), confirm yours conforms before opening the PR.

## Validate the input each check actually reads

Before trusting a passing result, identify whether the check reads the working
tree, the index, committed `HEAD`, or a composed merge tree. Confirm that its
file selection includes every new or renamed path in its scope.

Stage intended paths before index-based checks. Run `HEAD`-enumerating checks
after the local commit and before pushing or opening the PR: a scanner using
`git ls-tree HEAD` cannot see an uncommitted addition. When CI validates a
composed merge tree, use the repository's existing check for that result.

Prefer existing cheap native filename, structure, and metadata validators.
When file inclusion is unclear, use a focused control to verify the changed
path is checked. Reuse valid results for unchanged inputs where repository
policy permits; do not repeat a full suite merely to rerun a cheap enumeration
check.

For changes to a shared boundary, identify its affected consumers and run their
relevant existing suites, even outside the edited package. A success fixture
must satisfy the boundary's production contract; use small valid artifacts and
reserve malformed data for rejection cases. Passing each gate category does
not establish that the affected test scope ran.

## Why bespoke-first

Generic checks rarely fail twice; the **repo-specific** scanners and validators are where push-and-pray loses. Those are exactly the cheap-to-run, expensive-to-discover-remotely gates — running them locally is the highest-leverage minute you spend.

## Tell the visual story when the evidence already exists

When a UI change naturally produces useful before/after screenshots, golden images, or rendered design evidence, embed those images directly in the PR description with clear **Before** and **After** labels and one concise sentence explaining the visible outcome. Use durable GitHub-renderable URLs so the comparison remains visible after temporary CI artifacts expire.

Do not assume that a durable source URL is renderable. In access-controlled repositories, direct `raw.githubusercontent.com` and repository blob URLs may resolve for an authenticated contributor while GitHub's image renderer receives no authorization and shows a broken image. Upload evidence through GitHub's attachment flow when necessary and embed the resulting `github.com/user-attachments` URL, or use another durable URL that the rendered PR can retrieve.

After saving the PR description, inspect the rendered PR page rather than only the Markdown source or API response. Verify that each expected image completed loading with non-zero rendered dimensions (for example, `naturalWidth > 0` and `naturalHeight > 0`) and that the Before/After labels still map to the intended files. Repair broken evidence before calling the PR ready.

Treat this as a high-value opportunity, not a universal gate. Do not manufacture low-signal screenshots for every PR or delay delivery to create them. The rule is to surface strong visual evidence that already exists instead of burying it in CI artifacts, files, or comments.

## Pairs with

- `stacked-diff-discipline`: validate each layer on its actual parent and the selected stack's cumulative result. Bound runner use with verified workflow controls; draft status alone does not suppress CI. Qualify a ready prefix before group merge rather than waiting for every parent to merge first.
- Green locally is **not** done: done is merged + the artifact verified on the default branch, not a passing local run.

## DONE means

Before the PR opened, locally reproducible gates passed using the repository's
own entrypoints, including bespoke scanners and metadata validators.
Platform-only checks have explicit pending runs and must succeed before merge.
Avoid CI iterations on failures discoverable locally; investigate hosted or
infrastructure failures from actual evidence rather than claiming local proof.
When visual evidence was embedded, inspect the saved rendered PR and verify
every expected image loaded with non-zero dimensions.

Sources: shift-left / fail-fast testing practice; trunk-based development pre-merge checks; conventional-commit release gating.
