---
name: command-creator
description: Use when turning a repeated workflow into a Claude Code slash command. Structure the command markdown, choose argument handling and allowed tools, and write agent-executable steps so the workflow runs consistently instead of being re-explained every time.
---

# Command Creator

A slash command is a markdown file in `.claude/commands/` (project) or
`~/.claude/commands/` (user) that expands into a prompt when you type
`/name`. It turns a workflow you keep re-explaining into one consistent,
invocable unit. The skill is writing it so an agent *executes* it the same way
every time — not so a human reads it.

## When to make one

- A multi-step process you repeat (review, PR submission, fixing CI).
- A workflow that must run consistently regardless of who/when.
- An agent-delegation pattern worth standardizing.
- The tell: "I keep doing X — can we make that a command?"

## Structure

```markdown
---
description: One line shown in the command list
argument-hint: <pr-number>
allowed-tools: Bash, Read, Edit
---

Steps for the agent to execute, using $ARGUMENTS (or $1, $2) where the
invocation's text is substituted in.
```

- **`description`** — concise; it's how the command is discovered.
- **`argument-hint`** — what to type after the command.
- **`allowed-tools`** — scope tightly to what the workflow needs; don't grant
  broad access a command doesn't use.
- **Body** — imperative, agent-executable steps. `$ARGUMENTS` injects the
  invocation text.

## Write it for an executor, not a reader

- **Be imperative and specific** — "run the tests, then open a PR with the
  template" beats "you might want to test." The model follows steps; it doesn't
  infer intent from prose.
- **State preconditions and the done condition** — what must be true to start,
  and what "finished" looks like, so the command is verifiable.
- **One workflow per command.** If it branches into unrelated jobs, split it.
- **Push detail to references** — keep the command lean; link patterns/examples
  it can load when needed rather than inlining everything.

## Quality check before shipping

Invoke it on a real case and confirm it runs end-to-end without you narrating
the missing steps. A command that needs live coaching to work isn't done — that
coaching is the spec it's still missing.

---

_Adapted from the MIT-licensed [softaworks/agent-toolkit](https://github.com/softaworks/agent-toolkit) `command-creator` skill._
