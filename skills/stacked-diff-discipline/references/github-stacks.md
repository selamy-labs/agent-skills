# GitHub Stack Operations

Compose the official
[agent skill](https://github.com/github/gh-stack/tree/main/skills/gh-stack)
and [CLI reference](https://docs.github.com/en/pull-requests/reference/stacked-prs-cli-commands).
Check `gh stack <command> --help` for the installed version. Use explicit
arguments and machine-readable output; interactive commands may open a UI.

## Minimal authoring loop

Use an authenticated account with branch-write access to the target repository.
Use the same verified authentication context for reads and mutations.
Install the official extension if absent:

```bash
gh extension install github/gh-stack
gh stack --help
git config rerere.enabled true
git config remote.pushDefault origin
gh stack init refactor/contracts
# Implement and validate the first change, then commit explicit paths.
git add src/contracts.ts src/contracts.test.ts
git commit -m "refactor: extract request contracts"
gh stack add feat/validation
# Implement and validate the dependent change, then commit explicit paths.
git add src/validation.ts src/validation.test.ts
git commit -m "feat: validate requests against shared contracts"
gh stack submit --auto
gh stack view --json
```

`submit --auto` opens drafts with generated titles/bodies. Edit each description
to explain its full diff against its immediate parent and link the dependency.
Use `--open` when members are ready for review and CI; `--auto` is not auto-merge.
`--open` also readies existing members. For a partial prefix, mark only its
selected PRs ready with `gh pr ready <pr>`. Push/submit are not atomic; inspect
partial remote results after an error before retrying.

After editing a lower layer, run `gh stack rebase --upstack` from that layer,
resolve in the worktree named by the diagnostic, and push the reconciled stack.
Use `gh stack sync` to reconcile with GitHub; inspect resulting heads and
membership, not just exit code zero. Upstream documents a divergence case that
prints `Sync aborted` while exiting successfully.
`view --json` contains local saved base SHAs and best-effort remote refresh;
verify current GitHub heads, bases, checks, and membership before merging.

Once the selected prefix is qualified, inspect its explicit highest included
PR and use `gh stack merge <highest-included-pr> --yes --squash` if squash is the
approved method. A queue chooses its own method. Verify the remote result;
a successful request may only enqueue work.

## Adopt existing work without losing it

1. Inventory PR URLs, heads, bases, owners/worktrees, approvals, checks, and
   substantive dependencies. Separate independent work despite shared trackers
   or generated ledger conflicts.
2. Record recovery refs and commit SHAs. Coordinate owners and pause competing
   mutations; do not steal a checkout or discard dirty work.
3. Make intended branches linear using scoped rebases where needed.
   Retargeting alone does not remove unrelated inherited commits. Re-test
   conflict resolutions and changed descendant trees.
4. Adopt local branches with `gh stack init <bottom> <next> ...`, or link
   existing PRs with `gh stack link <bottom-pr> <next-pr> ... --remote origin`.
   `link` can retarget existing PRs and push branch arguments; it is a mutation,
   not a preview, and does not establish correct commit ancestry by itself.
   Resolve numeric targets first: an existing stack number can take precedence
   and append to that stack. Prefer verified branch arguments when ambiguous.
5. Read back native membership, order, incremental diffs, heads, and bases.
   Refresh affected review/CI evidence before selecting a merge prefix.

Keep original PRs and reviews whenever possible. Start a new stack for later
work rather than extending an already completed stack.

## CI and acceptance

GitHub Actions evaluates native members against the stack trunk for workflow
filtering. Inspect existing workflows before adding broader branch triggers.
Stack metadata is optional: guard its presence before reading `stack.position`,
`stack.size`, or `stack.base.ref`.

Keep meaningful per-layer checks. Expensive cumulative tests can run at the
top or lowest unmerged member when the repository's required-check contract
supports that arrangement. Do not replace required tests with skipped green
jobs. Ensure `merge_group` runs the required queue checks. Drafts alone do not
prevent runs.

Treat main certification and deployment separately from stack authoring.
A serial deployment gate need not serialize branch creation or review;
changing certification policy requires a reviewed repository change.

## When native stacks are unavailable

Confirm availability from the installed extension and repository response;
upstream exit code 9 denotes unavailable support. Report that evidence.
Do not infer unavailable support from a missing extension or one auth failure.

Use ordinary parent-based PRs or an existing compatible tool with the same
dependency manifest and evidence discipline. Fall back to bottom-first
individual merges with explicit descendant rebases/retargets; this path has no
native group-merge guarantee. Independent PRs remain flat. Preserve genuine
host checks and approvals.

Without native stacks, each layer merges only its own parent branch, never
trunk directly; trunk changes flow up from the bottom layer. After a parent
merges, retarget its open children to the parent's base before deleting the
parent branch. A merge into a non-default branch does not close linked issues,
so when the stack lands on the default branch, close the issues linked from
every layer merged into it.
