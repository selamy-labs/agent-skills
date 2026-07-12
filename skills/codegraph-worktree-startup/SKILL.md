---
name: codegraph-worktree-startup
description: Use when starting or resuming code work in a repository worktree. Initialize or sync CodeGraph, keep its cache uncommitted, and prefer CodeGraph for code navigation before grep or broad file reads.
---

# CodeGraph Worktree Startup

Use CodeGraph as repository startup hygiene for coding tasks. The goal is faster, more accurate code navigation without committing `.codegraph/` cache artifacts.

## Startup

1. Confirm the tool and repository root:

```bash
command -v codegraph
git rev-parse --show-toplevel
```

If `codegraph` is unavailable, say so briefly and use normal repo exploration.

2. If an index exists, sync it:

```bash
codegraph sync "$(git rev-parse --show-toplevel)"
codegraph status "$(git rev-parse --show-toplevel)"
```

3. If no index exists, initialize one before code exploration unless disk, memory, or repo size makes that unreasonable:

```bash
/usr/bin/time -f 'elapsed=%E cpu=%P maxrss_kb=%M' codegraph init "$(git rev-parse --show-toplevel)"
codegraph status "$(git rev-parse --show-toplevel)"
```

Local calibration has shown small and medium repositories can index in seconds, so prefer measuring once over assuming indexing is expensive.

4. Keep CodeGraph out of source control:

```bash
printf '\n.codegraph/\n' >> "$(git rev-parse --git-path info/exclude)"
git status --short --branch
```

Use local git excludes instead of editing `.gitignore` unless the repository explicitly wants a shared ignore rule. `git rev-parse --git-path info/exclude` works in linked worktrees because it resolves the real git metadata path.

## Usage

Use CodeGraph before grep or broad file reads when locating code, call paths, blast radius, or likely tests:

```bash
codegraph explore "<symbol, file, behavior, or bug area>"
codegraph node "<symbol-or-file>"
codegraph affected <changed-files>
```

Prefer:

- `codegraph explore` for relevant symbols, line-numbered source, call paths, and blast radius.
- `codegraph node` for one exact file or symbol.
- `codegraph affected` after edits when choosing focused tests.
- `rg` after CodeGraph for text outside the code index, such as docs, YAML literals, shell snippets, issue numbers, or generated config.

Run `codegraph sync` after meaningful edits before relying on impact or affected-test output.

## Reporting

When timing a new repository class, record wall time, indexed files, DB size, memory, and whether the first query was useful. If indexing is slow or low-value for that class, document the exception instead of making the startup step performative.
