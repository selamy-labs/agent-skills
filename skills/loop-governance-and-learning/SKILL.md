---
name: loop-governance-and-learning
description: Use after an iteration, bug, review, incident, or repeated failure to decide what durable artifact should change: tests, skills, docs, decision logs, issues, or nothing.
---

# Loop Governance And Learning

Use this after a meaningful loop finishes: a bug is fixed, a review changes the
direction, a workflow succeeds, an incident exposes a gap, or a repeated
failure reveals a pattern. The goal is to convert durable learning into the
right artifact so the same lesson does not stay trapped in a transcript.

Not every lesson deserves persistence. Capture reusable knowledge; discard
temporary task state.

## First Classify The Learning

Name the lesson in one sentence, then classify it:

| Lesson type | Durable artifact |
| --- | --- |
| A bug can recur | Regression test, fixture, monitor, or policy check |
| A behavior promise is unclear | Acceptance check, example, or product issue |
| A workflow is repeatable | Skill, runbook, or checklist |
| A prior decision matters | Decision log, commit message, or PR description |
| A source assumption changed | Documentation or source-of-record update |
| A task was merely completed | No durable artifact |

If the lesson is not reusable, do not persist it. Stale memory is worse than no
memory because future agents treat it as fact.

## Governance Loop

1. **Extract the lesson.** Separate the durable pattern from incidental task
   details, credentials, private names, timestamps, branch names, and one-off
   state.
2. **Choose the substrate.** Decide whether the lesson belongs in a test, skill,
   doc, issue, decision log, monitor, or no artifact.
3. **Prefer strengthening an existing artifact.** Patch an existing skill, test,
   or doc when the new lesson refines a known workflow. Create a new artifact
   only when the gap is recurring, named, and not covered elsewhere.
4. **Attach evidence.** Link the artifact to source-backed evidence: failing
   test, reproduction, review comment, incident note, trace, or decision record.
5. **Verify the artifact.** Run the check, validate the skill, read the rendered
   doc, or confirm the issue/decision log captures the right next action.
6. **Report the change.** State what was learned, what artifact changed, why
   that substrate was chosen, and what was intentionally not persisted.

## When To Update A Skill

Use [[skill-curation]] before creating or changing a skill. A skill update is
appropriate when:

- the behavior is reusable across more than one task or repository;
- the procedure is non-obvious enough that future agents would otherwise
  rediscover it;
- the trigger can be named cleanly in the frontmatter description;
- the skill can be public-safe or clearly scoped to its intended audience; and
- the update does not duplicate a sharper existing skill.

Do not create a skill for:

- a single task's progress;
- an environment-specific path, person, host, credential, or queue;
- a vague slogan with no operational steps;
- a temporary workaround; or
- knowledge that belongs in tests or source docs instead.

## When To Update Tests Or Evals

Prefer a test, eval, or monitor when the lesson is about observable behavior.
Use [[regression-ratchet]] for bugs and [[feature-coverage-not-just-line-coverage]]
for user or system promises.

The check should fail on the bad behavior and turn green after the fix. If it
cannot fail independently, it is not evidence; it is a restatement of the
implementation.

## When To Update History Or Docs

Use [[source-history-decision-log]] when the lesson is a rationale: why a shape
was chosen, why an alternative was rejected, or what prior failure a line of
code protects against.

Use docs when the lesson explains how to operate, configure, or understand a
system. Keep docs close to the source they explain, and remove stale statements
instead of layering contradictions.

## Stop Conditions

Stop with a durable-learning change when:

- the reusable lesson is captured in the right artifact;
- the artifact is verified at the cheapest meaningful layer;
- sensitive or task-local details were removed; and
- the report distinguishes durable learning from temporary state.

Stop without changing durable artifacts when:

- an existing artifact already covers the lesson;
- the lesson is only task progress;
- the evidence is too weak to generalize;
- the user asked not to persist it; or
- the proposed artifact would leak private context.

## Output Shape

```text
Lesson: <durable pattern learned>
Artifact: <test, skill, doc, issue, decision log, monitor, or none>
Why this substrate: <short rationale>
Verification: <how the artifact was checked>
Not persisted: <task-local or unsafe details intentionally left out>
```

## Anti-Patterns

- Turning every completed task into memory.
- Storing branch names, PR numbers, local paths, or timestamps as durable facts.
- Creating a new skill when an existing skill only needed one sharper sentence.
- Writing a lesson without evidence from the loop that produced it.
- Capturing private context in public artifacts.
- Treating a transcript summary as a substitute for a test, doc, or decision log.
