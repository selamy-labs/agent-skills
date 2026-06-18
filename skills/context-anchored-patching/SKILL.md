---
name: context-anchored-patching
description: Use when patching code you do not fully control or that moves between runs. Anchor edits on unique surrounding context and verify by behavior, never by line number or a grep that only confirms the text is present somewhere.
---

# context-anchored-patching

The core failure: a patch that is correct in content lands in the **wrong scope**. Against code that moves (upstream you re-fetch, generated files, a file another change just edited), a line-number diff applies to whatever now sits at that line, and a bare-pattern match applies to the first of several occurrences. A grep afterward says "the text is there" and the patch looks done, while the behavior is broken. Anchor on context; verify by behavior.

## Anchor on unique surrounding context

- Match the change by the **lines immediately around it**, not by line number. Line numbers drift the moment anything above them changes.
- If the anchor is not unique (the same token or block appears more than once), **expand the context** until exactly one location matches. A function name, a `return`, or a closing brace alone is rarely unique.
- If you cannot make the anchor unique, the patch is **ambiguous: stop and fail, do not guess** a location. Guessing is how a change lands in the wrong scope.
- For moving upstream or generated targets, **re-fetch the current content right before patching** so the anchor reflects reality, not a stale copy.

## Verify by behavior, not by grep

- `grep` confirms a string is *present somewhere*. It does not confirm it is in the *right place* or that the result is correct. A wrong-scope patch passes a grep check and fails the build.
- Run the real behavior the change affects: the build, the test, the command, the rendered output. "It applied cleanly" and "the text is there" are not done; **the behavior passing is done** (see `verify-real-artifact`).

## The failure to reproduce (documented fixture)

A config has two blocks that both contain `enabled: false`. A patch anchored on `enabled: false` alone flips the **first** block; the intended target was the second. A grep for `enabled: true` now succeeds (it is present), so the patch reads as done, but the wrong feature was toggled. The fix: anchor on the second block's **unique** surrounding keys, and verify by running the behavior that the second block controls. A test asserting that behavior fails on the wrong-scope patch and passes on the anchored one. That test is the guard.

## Checklist

- [ ] Anchor is unique surrounding context, expanded until exactly one match.
- [ ] Ambiguous (multi-match) anchor fails the patch rather than picking one.
- [ ] Moving/generated target re-fetched immediately before patching.
- [ ] Verification runs the affected **behavior** (build/test/output), not a grep for the changed text.
- [ ] A regression test asserts the behavior, so a future wrong-scope patch is caught (regression-ratchet).

## DONE means

The change is anchored on unique context (or failed as ambiguous, never guessed), the affected behavior was run and passes, and a test now asserts that behavior so a wrong-scope re-patch is caught next time. Not "the diff applied" and not "grep finds the string."

Sources: three-way-merge / contextual-hunk practice (diff/patch, git apply --3way); behavior-over-presence verification.
