---
name: readme-excellence
description: Use when creating or auditing a repository README. A README earns its keep — every section answers a real reader question, the quickstart actually runs, and a diagram appears only when it replaces prose.
---

# readme-excellence

A README is the repo's first and most-read interface. The bar is **clarity that earns its keep**, not length: every section answers a real reader question, or it is deleted or linked out. Longer is not better.

## The section spine (order; omit optional, never reorder)

1. **Title** — matches the repo/package; one-line (<120 char) description of what it is.
2. **What & Why** — 2–4 sentences: the problem it solves; who/what consumes it. A reader knows this within the first screen.
3. **Quickstart** — the smallest sequence that *actually runs* end-to-end, with expected output.
4. **Install / Requirements** — deps, versions, OS; secrets injected at runtime, never inline.
5. **Usage** — common cases with real code blocks + expected output; link large examples out.
6. **Architecture** *(optional)* — only if it earns its keep; this is where a diagram may go (see below).
7. **Configuration** — point to the source of truth (flags / `--help` / config schema); don't duplicate values that will rot.
8. **Development** — clone → build → test → lint commands that succeed exactly as written.
9. **Contributing** — PR policy, required CI, coverage gate.
10. **License** — last.

## The "earn its keep" rules

- Every section answers a real reader question or it is **deleted or linked out** — don't mix reference, tutorial, and explanation in one README.
- No filler, no aspirational/un-shipped capabilities, no restating the obvious.
- A reader reaches a runnable state from **Quickstart alone** — if they can't, the quickstart is the defect (treat a non-running quickstart like a failing test).
- Reference detail (full API, design rationale, tutorials) **links out**; the README stays a reference + one how-to.
- Badges are **real and live** (CI, coverage, license, version) — no vanity badges.

## Mermaid: when a diagram is justified

A diagram is ONE component, included **only when it replaces prose a reader would otherwise reconstruct** — system/component architecture, data/request flow, a state machine, a non-obvious cross-service sequence. **Skip it** when it just restates a list, the repo is a single module, or it would exceed ~15 nodes (split it, or move it to dedicated docs). Use text-based mermaid (renders + reviews in PRs); never commit a diagram image.

## DONE means

A reader knows what the repo does and why within the first screen; the **quickstart runs as written** and shows its output; every section earns its keep with reference detail linked out; config points to the source of truth, not a rot-prone copy; any diagram replaces reconstructable prose and is ≤~15 mermaid nodes; sections follow the spine order. Output of an audit is a section-by-section verdict plus a concrete diff, not commentary.

Sources: standard-readme spec (RichardLitt); makeareadme.com; Diátaxis documentation framework (Procida).
