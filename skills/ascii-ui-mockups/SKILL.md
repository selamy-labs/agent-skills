---
name: ascii-ui-mockups
description: Use when exploring or communicating a UI layout before writing code. Sketch the screen as a text mockup — boxes, fields, labels — and offer a few distinct variations, so the layout decision is made cheaply in a PR or chat before anyone builds it.
---

# ASCII UI Mockups

A text mockup answers "what goes where" in seconds, inside a PR description or
chat, with zero tooling and zero implementation cost. It's the cheapest possible
place to have the layout argument — before components exist.

## When this beats a real prototype

- Aligning on layout/IA before committing to implementation.
- Showing two or three structural options so someone can pick, not react.
- Embedding a layout in a PR, issue, or ADR where an image wouldn't version.

If pixel-level visual design or interaction feel is the question, a mockup
won't answer it — build the real thing.

## How to do it well

1. **Restate the requirement first** — the components, the data shown, the
   hierarchy — so the sketch targets the actual need.
2. **Offer 2–4 distinct variations**, not one. The value is in contrasting
   structural approaches (sidebar vs top-nav, table vs cards), each with one
   line on its trade-off.
3. **Use a consistent box vocabulary** so it reads cleanly:

```
+----------------------------------------------+
| Logo            Search [____________]  (User)|
+--------+-------------------------------------+
| Nav    | Dashboard                           |
| - Home | +-------------+  +----------------+  |
| - Stats| | Active: 1,204|  | Revenue chart  |  |
| - Bills| +-------------+  |   /\   /\       |  |
|        | +----------------------------------+ |
|        | | Recent orders (table)            | |
|        | |  #  Customer     Total   Status  | |
|        | | 01  A. Rivera    $42.00  Paid    | |
+--------+-+----------------------------------+-+
```

4. **Label interactive elements** — `[Button]`, `[____]` input, `(o)` radio,
   `[x]` checkbox, `(User)` menu — so structure and affordance are both legible.
5. **Number the options and ask for a pick.** Be ready to merge elements from
   two, or refine the chosen one.

## Hand off cleanly

Once a layout is chosen, translate it into a component breakdown and build
order — the mockup becomes the blueprint the implementation refers back to, so
the structure decision doesn't get relitigated mid-build.

---

_Adapted from the MIT-licensed [softaworks/agent-toolkit](https://github.com/softaworks/agent-toolkit) `ascii-ui-mockup-generator` agent._
