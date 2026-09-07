---
name: codegraph-worktree-startup
description: Use when starting or resuming code work in a repository. Verify CodeGraph, initialize or sync the local graph, keep indexes uncommitted, and prefer graph queries before broad file reads.
---

# CodeGraph Worktree Startup

Use CodeGraph as repository startup hygiene for coding tasks. The goal is
faster, more accurate code navigation without committing local index artifacts.

## Startup

Confirm the tool and repository root:

```bash
command -v codegraph
git rev-parse --show-toplevel
```

If `codegraph` is unavailable and the user has not authorized installing it,
say so briefly and use normal repository exploration.

Keep the generated index out of source control:

```bash
root="$(git rev-parse --show-toplevel)"
grep -qxF '.codegraph/' "$(git rev-parse --git-path info/exclude)" || printf '\n.codegraph/\n' >> "$(git rev-parse --git-path info/exclude)"
```

If an index already exists, sync it before relying on it:

```bash
codegraph sync "$root"
codegraph status "$root"
```

If no index exists, initialize one before code exploration unless the repository
is clearly too large for the current disk/time budget, the task is not code
work, or the user asked not to create an index:

```bash
codegraph init "$root"
codegraph status "$root"
```

## Usage

Use CodeGraph before grep or broad file reads when locating code, call paths,
blast radius, likely tests, or a symbol's implementation:

```bash
codegraph explore "<symbol, file, behavior, or bug area>" --path "$root"
codegraph node "<symbol-or-file>" --path "$root"
codegraph affected <changed-files>
```

Prefer:

- `codegraph_explore` MCP when it is available, because it returns relevant
  source, call paths, and blast radius in one tool call.
- `codegraph explore` for relevant symbols, line-numbered source, call paths, and blast radius.
- `codegraph node` for one exact file or symbol.
- `codegraph affected` after edits when choosing focused tests.
- `rg` after CodeGraph for text outside the code index, such as docs, YAML literals, shell snippets, issue numbers, or generated config.

Run `codegraph sync` after meaningful edits before relying on impact or affected-test output.

## Reporting

If CodeGraph cannot be installed, initialized, or queried, state the specific
blocker and fall back to ordinary repo exploration. Treat `.codegraph/` as
local generated state unless a repository explicitly asks to version it.
