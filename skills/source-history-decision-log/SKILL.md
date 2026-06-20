---
name: source-history-decision-log
description: Use before changing code with a history — especially fragile, recurring-issue, or high-stakes code. Mine the git/PR history as a decision log: learn why the code is the way it is and avoid repeating solved mistakes; reading history is how you iterate instead of re-deriving.
---

# Source-History Decision Log

Source control isn't only a safety net — it's the project's **decision log**. The code's current shape is the residue of past decisions; most odd-looking lines are fixes for something that already bit someone. Read the history *before* you change code, so you build ON accumulated learning instead of re-deriving dead-ends and re-introducing solved bugs.

**Reading history is how you iterate. Skipping it is how you oscillate** — fixing a symptom, hitting the next, sometimes re-breaking the first.

## The one practice that matters most

Before changing fragile / recurring / high-stakes code: `git blame` the lines and read the last few relevant commits + PRs, then state in your change **what prior decision you are preserving or deliberately reversing.** If you can't say *why* the code is the way it is, you're not ready to change it.

## The habit

1. **Blame before you change.** `git blame` / `git log -p` the specific function. Don't undo a fix you don't understand.
2. **Follow the "fix-the-fix" chains and reverts.** A chain of `fix:` commits or a revert marks a landmine that has bitten before — read each; the history names the failure modes.
3. **Read PRs, not just commits.** Descriptions and review threads carry the *why* and the rejected alternatives the diff can't show.
4. **Check before you re-try.** `git log --all --grep=<concept>` — a reverted commit is a tried-and-failed approach; read why before re-discovering the dead-end.
5. **Fix the class, not the instance.** When fixing a bug, search history for the same *class*. If one fragility recurs in different places, the real fix is the class — and a prior fix's message often *already names the pattern* you're about to re-learn the hard way.
6. **Write the log you'd want to read.** Commit and PR messages are the decision log for the next person. Capture the *why*, the alternatives, the rationale — never "wip" / "fix". Read to learn; write to teach.

## When to use / when not

- **Use it** for fragile, recurring-issue, live, security, or otherwise high-stakes code; before "fixing a fix"; before re-trying something; and whenever you — or a fresh session, or a different teammate — lack the context a long-tenured maintainer would carry in their head.
- **Don't over-mine.** Greenfield or trivial changes need little archaeology. Target the specific lines and the relevant chain, not the whole repository's history. Scale the effort to the stakes, the same way you scale testing.

## Why it matters

The current shape encodes hard-won lessons. A prior commit message often *already names the pattern* you are about to re-learn — generalizing it sooner is the entire point of iterating. For a team — and especially for many contributors working across separate sessions — the history is the shared long-term memory: reading it is how a context-blind newcomer acquires the maintainer's intuition instead of re-deriving it badly.
