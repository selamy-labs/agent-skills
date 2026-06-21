---
name: typed-rewrite-migration
description: Use when migrating an untyped or legacy core to a typed/rewritten one without a risky big-bang cutover. Strangle it incrementally behind a stable boundary, run old and new in parallel to prove equivalence, and cut over slice by slice while both stay green.
---

# Typed-Rewrite Migration

A big-bang rewrite of a system that's in use is how you ship a quieter, more
confident outage. The safe path is a **strangler**: stand the new typed core up
behind a boundary, move one slice at a time, and prove equivalence with the old
one before you trust it. The migration is reversible at every step until the
last.

## The playbook

1. **Draw the boundary first.** Put a stable interface (a function seam, a
   proto/API contract, an adapter) between callers and the thing you're
   rewriting. Nothing downstream should know which implementation answers.
2. **Parse at the edge into the new types.** Inbound data becomes typed values at
   the boundary (parse-don't-validate); the new core only ever sees legal states,
   so the rewrite isn't also re-litigating validation.
3. **Run old and new in parallel (shadow).** For each request, run both, return
   the old result, and *compare* — log every divergence. This is where
   property/oracle tests shine: the property is "new agrees with old for all
   inputs." Divergences are your real spec, written down at last.
4. **Cut over per slice, behind a flag.** When a slice's divergences are zero (or
   explained and accepted), flip it to the new path — runtime-tunable, no
   redeploy. Keep the old path one flag-flip away.
5. **Delete the old slice in the same change that proves the new one.** Adopt-
   and-delete: the cutover commit removes the superseded code so there's never a
   limbo of two live implementations drifting apart.

## Why each step matters

- The **boundary** is what makes it incremental instead of all-or-nothing.
- **Parallel-run** turns "I think it's equivalent" into evidence, and surfaces
  the undocumented behaviors every legacy system has.
- **Per-slice flags** keep blast radius tiny and rollback instant — the cutover
  is a config change, not a deploy.
- **Delete-on-cutover** prevents the most common rewrite failure: a half-migrated
  system maintained in two places forever.

## Smells

- "We'll switch everything over on release day" — no parallel-run, no rollback;
  you're betting the system on untested equivalence.
- New and old both live for months with no deletion date — the rewrite became a
  permanent tax instead of a migration.
- Cutover gated on a redeploy instead of a flag — slow, coarse, scary rollbacks.
