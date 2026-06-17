---
name: low-level-executor-task-spec
description: Use when dispatching a task to an autonomous executor or sub-agent that does not share your context. Spell out exact paths, the literal access command, naming and identity conventions, and known gotchas — assume zero tribal knowledge.
---

# Low-Level Executor Task Spec

An autonomous executor starts with none of the context in your head. It cannot
see your repository layout, guess which credential opens which door, infer the
name of the required check, or know the identity a commit must carry. Whatever
you leave implicit, it must either invent or fail on — and a confident invention
is the more expensive outcome, because it looks like progress until you verify
it.

A vague delegation produces vague or wrong work. A precise spec — exact file
paths, the literal command that grants access, the naming and identity
conventions, the gotchas with their required formats — produces correct work on
the first try. The discipline is to write the spec as if for someone who knows
the methodology but has never seen your system.

## The Rule

Specify every concrete value the executor cannot infer. Paths, commands,
names, identities, and required formats are inputs to the task, not background
knowledge. If a detail is needed to finish the work and the executor cannot
derive it from what you handed over, it belongs in the spec.

## Why Executors Need This

- **They have no view of your layout.** "The config file" or "the usual repo"
  resolves to nothing without the path. The executor will guess, and a plausible
  wrong path is worse than an error.
- **Access is not discoverable.** Which token, which account, which login —
  these are not visible from inside the task. The literal access command must be
  in the spec.
- **Conventions are invisible.** Branch naming, commit identity, title formats,
  and directory structure are tribal knowledge until written down.
- **Gotchas are unteachable in hindsight.** A required PR-title format or a check
  that does not re-trigger on edit is cheap to state up front and costly to
  discover after a failed run.
- **Success is ambiguous without a definition.** Without an explicit success
  criterion and a way to verify it, the executor self-grades and reports done
  too early.

## Spec Checklist

Include every item the executor cannot infer on its own:

- **Exact paths, not descriptions.** Write the full path to each file or
  directory to read, create, or change — never "the config" or "the right
  folder".
- **The literal access command.** Paste the exact command that grants access
  (the auth prefix, the credential lookup, the clone or fetch invocation), not
  "authenticate first".
- **Naming conventions, spelled out.** The branch name, the directory name, any
  required prefix or suffix — give the literal string or the exact rule.
- **The commit and authorship identity.** The name and email a commit must
  carry, and any required trailer or co-author line, verbatim.
- **Required formats and their gotchas.** Title formats, message conventions,
  and any rule that a downstream gate enforces — including ordering gotchas
  (for example, a value that must be correct at creation time because editing
  it does not re-trigger the check).
- **The exact validation step.** The literal command or check to run locally
  before shipping, so the executor self-verifies against the same gate the
  pipeline will apply.
- **The success criterion.** A concrete, checkable definition of done — what
  artifact must exist and in what state — not "make it work".
- **What NOT to assume.** Call out the boundaries: which files to leave
  untouched, which scope not to widen, which adjacent work is out of bounds.

## Before / After

Vague delegation, which forces guessing:

> Add the new skill to the skills repo, make a branch, and open a PR. Make sure
> CI passes.

Precise delegation, which can succeed first try:

> Clone the public skills repo with `<auth-prefix> <clone-command> /tmp/work`.
> Create only `skills/example-skill/SKILL.md` (do not edit any existing file).
> Branch `skill/example-skill`. Commit as `user.name=ci-bot`,
> `user.email=ci-bot@example.invalid`, body ending with the required co-author
> trailer. Push with `<auth-prefix> <push-command>`. The PR title must be a
> lowercase conventional commit — `feat: add example-skill` — and must be
> correct at create time, because editing the title does not re-trigger the
> title check. Validate locally first with `<lint-command>`. Done = PR open with
> all required checks green and auto-merge enabled.

The second version removes every place the executor would otherwise invent a
value.

## When To Use

- Dispatching a unit of work to a sub-agent, worker, or any process that does
  not share your context.
- Writing a task brief that another party will execute without a chance to ask
  follow-up questions.
- Any delegation where a wrong-but-plausible result would be merged or built on.

## When Not To Use

- Work you will execute yourself with full context in hand.
- A throwaway exploration where a wrong guess costs nothing and nothing
  downstream depends on the result.

## Anti-Patterns

- Referring to "the config", "the repo", or "the usual place" instead of a path.
- Saying "authenticate" or "use the right credentials" without the literal
  command.
- Leaving branch names, commit identity, or title formats to the executor's
  judgment.
- Omitting a known gotcha because it "should be obvious".
- Ending with "make it work" instead of a checkable success criterion.

## Done Standard

The spec is complete when an executor with the relevant skills but zero
knowledge of your system could finish the task from it alone — every path,
command, name, identity, format, and success criterion is on the page, and
nothing load-bearing is left to inference.

Pair this with [[dispatch-lane]] for routing the work to the right executor,
and with [[verify-delegated-work]] for checking the result once it comes back —
a precise spec reduces wrong work, but the returned artifact still gets
independently verified.
