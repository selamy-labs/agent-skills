---
name: skill-curation
description: Use when deciding whether and where a skill should exist, how to scope its audience, how to avoid duplicates, and whether it belongs public or internal. This is curation (scope, place, dedup, share), not how to write one.
---

# Skill Curation

How to **scope, place, deduplicate, and share** skills so a fleet builds a coherent library instead of a pile of overlapping ones. This **composes with** skill-creator / `writing-skills` (which teach how to *author* a skill) — it covers the decisions *around* authoring, which they don't.

## 0. Composition-first (do this before writing anything)
**Check for an existing skill before authoring a new one.** The best skills usually already exist publicly. Order of preference: **reuse > compose/extend an existing skill > author new.** Author new only for a genuinely-uncovered gap, and say what you searched. (Reinventing a skill that already exists publicly is the most common curation failure.)

## 1. Audience-scoping
Every skill declares an **audience**: `control-plane-only` | `worker` | `universal`.
- Bundles compose **only the right audiences** — a control-plane-only skill must never ship into a worker's bundle, and vice versa.
- This prevents the classic leak: operational control-plane skills (queue management, dispatch) reaching worker agents that should never see them. Scope at authoring time; enforce at bundle-composition time.

## 2. Dedup + precedence (one name → exactly one skill)
- A skill **name resolves to exactly ONE skill.** Never ship an ambiguous/colliding name.
- When the same capability appears in multiple roots/sources, define **precedence** (which root wins) and **reconcile** the duplicates — don't double-bundle two skills that do the same thing.
- Before adding a skill from an external source, diff it against what you already have; if it overlaps, dedup/merge rather than stack.

## 3. Public-first ladder
Default to the **most-shared** home that's safe:
1. **Public** skills repo first (generically useful, no org secrets) — past a **privacy-scanner gate**.
2. **Thin internal wrapper** over the public skill if internal specifics are needed.
3. **Dense internal** skill only as a last resort (truly internal-only content).

The public skill carries **zero org traces** — its text stands entirely on its own, with no reference to any non-public system or its existence. Public skills are a differentiator and a visibility flywheel; protect the brand with a **gold-only bar** (slop kills the flywheel).

## 4. Naming hygiene
- **kebab-case PURPOSE names** (`rest-resource-modeling`, not `acme-api-helper`).
- **No org prefixes** (no `acme-…`).
- **No stale tool names** (rename when a tool is renamed; keep dev/bundle names consistent).
- The name states what the skill is *for*, readable cold.

## 5. The graduation bar (when a skill earns promotion)
A skill moves draft → internal → public only when: **proven-in-use** (which user/agent, what outcome), **shape-stable**, **fully sanitized**, **named per the hygiene rules**, and it **passes its efficacy evals** (ships with eval cases proving a behavioral delta). Extremely high bar for public; when in doubt, internal-only.

## When to use / not
- **Use** when deciding if/where/how a skill should exist, or auditing a skills library for overlap/leaks.
- **Not** for *writing* the skill body — that's skill-creator / `writing-skills`.

## Anti-patterns
- Authoring a skill without checking for an existing one (reinvention).
- A control-plane-only skill leaking into worker bundles (audience not scoped).
- Two skills with the same/ambiguous name, or two skills doing the same thing in different roots.
- Org-prefixed or stale-tool names; a public skill that references internal systems.
- Promoting to public on elegance instead of proven-in-use + passing evals.
