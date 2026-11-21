# Contributing

This repository is a stable public skill catalog. Keep additions few, sharp,
and reusable without local context.

## Skill Naming

- Use lowercase descriptive kebab-case.
- Name the practice or workflow, not the author, organization, model, or tool.
- Do not add vendor prefixes, brand names, `claude-`, `ai-`, or similar noise.
- Prefer a noun phrase for a discipline, such as `process-aware-done`, or a
  verb-first workflow name when the skill is an ordered procedure.
- Keep names stable once published. Removing or renaming a skill is a breaking
  public API change.
- Check upstream catalogs before adding a name. If an upstream skill already
  uses the same name, only reuse it when this repository intentionally provides
  the higher-precedence version, and document that decision in the same change.

## Graduation Bar

A skill belongs here only when it changes agent behavior on real work and a
stranger could use it without our operating context. Incubate rough workflows
elsewhere until the name, frontmatter, structure, and behavior are settled.

Do not publish slogans. Publish compact workflows, checklists, or verification
rules that materially change the result.

## Architecture Documentation

Architecture diagrams MUST be updated in the same PR that changes the behavior
or spec they describe.

## Development Setup

Use Python 3.11 or newer on a POSIX system. For each fresh clone:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m tools.git_hooks install
```

The source bootstrap installs pre-commit and pre-push hooks with the pinned
native prek version. It copies the verified executable and a small runtime guard
into the common Git directory, so the hooks do not depend on the temporary
Python environment. Python 3 must remain available. Missing or changed engine
bytes produce a configuration error; there is no fallback to another executable.
The existing `.pre-commit-config.yaml` remains the check definition.

Worktrees sharing Git's default hook directory inherit this lifecycle. The
installer respects `core.hooksPath`; when that setting selects a separate hook
directory for each checkout, run the bootstrap in each author worktree. Git does
not transfer hooks into a fresh clone, so setup must include the command above.

Installation holds an owner-checked, nonblocking lock in the common Git directory.
It stages both native hooks and checks existing hook, legacy and pin state before
publishing. Foreign hooks migrate through native prek's legacy
chaining. A foreign hook with an existing legacy backup, a symlinked state path,
or state changed during staging requires reconciliation before retrying.

Pre-push accepts one source update matching the clean checked-out HEAD, with
optional deletions, or a deletion-only/empty batch. Source tags, multiple source
branches, malformed input and non-HEAD source tips are rejected before any check
verdict. The original remote arguments and complete input reach native prek and
any migrated legacy hook. Split unsupported source updates into separate pushes.
Re-run the installer after updating the maintained bootstrap or pinned version.
