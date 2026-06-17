---
name: adopt-and-delete
description: Use when adopting a shared module, library, chart, or service, or doing any consolidation, migration, or refactor that supersedes existing code. The change that adds the replacement must REMOVE what it replaced, in the same atomic change.
---

# Adopt and Delete (no dead code)

When you adopt a shared module, library, chart, or service — or consolidate duplicates — **delete the code it supersedes in the same change.** The change that introduces the replacement must remove what it replaced. No orphaned, dead, or duplicated code left behind.

## The rule
A migration is not "add the new path." It is **"add the new path AND remove the old one,"** as one atomic, reviewable unit. If the old code still compiles and runs after your change, you adopted *and forked* — you did not adopt-and-delete.

## Why (the failure mode this prevents)
Leftover duplicates don't sit quietly — they rot:
- **Drift**: the old and new copies diverge; bugs fixed in one reappear from the other.
- **False-done**: "migrated to the shared lib" reads as complete while the inlined copy is still the one actually running.
- **The clean footgun**: a `git clean` / fresh checkout deletes the untracked-or-half-removed remnants and silently changes behavior.
- **Ambiguity**: the next contributor can't tell which path is canonical, so they edit the wrong one.

Dead code is not free. It is a standing liability that costs every future reader.

## How to do it
1. **Find every caller** of the thing you're replacing before you start — that set is your deletion checklist, not an afterthought.
2. **Switch callers to the new path.**
3. **Delete the superseded code** — the old module, the inlined copy, the duplicate chart, the now-unused config — in the same commit/PR.
4. **Grep for stragglers**: references, imports, dead flags, docs pointing at the old path. Zero hits is the done bar.
5. **One concern per change**: if the deletion is large, the adoption and the deletion still ship together — split by *area*, not into "add now, delete later." "Delete later" is how dead code is born.

## Done bar
- The replaced code no longer exists in the tree.
- No reference, import, or doc points at it.
- The build/tests pass *because* of the new path, not because both still exist.

## When to use / not
- **Use** for migrations, consolidations, shared-module adoption, dedup, refactors that supersede code.
- **Not** a reason to delete code that is still a live, separate concern — only delete what your change actually replaces. Removing unrelated code is a different change.

## Anti-patterns
- "Add the new path now, delete the old one in a follow-up" — the follow-up never lands; the dupe drifts.
- Leaving the old module importable "just in case" / "for backwards-compat" with no caller and no deprecation plan.
- Commenting out the old code instead of deleting it (version control already remembers it).
- Marking a migration done while the old copy is still the one in the running path.
