---
name: authoring-skills-vs-mcp
description: Use when deciding whether a capability should be a SKILL or an MCP SERVER, and how to author either well. The axis is knowledge-vs-capability: would the agent READ this to know how, or CALL this to do it?
---

# Authoring: Skills vs MCP Servers

Two different ways to extend an agent, for two different things. Picking the wrong one is the most common waste: knowledge bloated into a server, or a real capability trapped in prose the model can only read, never call. This skill is the **decision axis** plus how to author each artifact. It **composes with** skill-creator / `writing-skills` (authoring mechanics) and `mcp-builder` (server mechanics) — it covers the *choice between them*, which they don't.

## 1. The decision axis (apply this first)
- **SKILL = knowledge.** Methodology, convention, domain knowledge the model **reads into context** and then acts on with the tools it already has. There is nothing to "call."
- **MCP SERVER = capability.** An external system / API / DB / service that needs **credentials and structured I/O** — a **callable tool** the model invokes to *do* something and get a typed result back.

**The test:** *"Would the agent READ this to know how, or CALL this to do it?"*
- Read-to-know-how → **skill**. (e.g. "how to model a REST resource", "how we review for regressions".)
- Call-to-do-it → **MCP**. (e.g. "search this ticketing system", "write a DNS record", "query the metrics warehouse".)

Two failure modes to reject:
- **Don't MCP-ify knowledge.** A methodology doesn't become a server because it's important. It has no creds, no structured call — it's a skill.
- **Don't trap a capability in prose.** A skill that says "here's how to hit API X: curl this, parse that" is a capability wearing a skill costume. Extract it into an MCP tool; leave at most a thin methodology skill behind.

Hybrids are real: keep the **methodology** as a skill and the **call** as an MCP (e.g. a "research discipline" skill beside a "search the source" MCP). Split along the read/call line.

## 2. How to author a SKILL
- **Frontmatter**: `name` (kebab-case purpose), a `description` that says *when to use* (and when not) so it's discoverable cold.
- **Progressive disclosure**: lead with the decision/the rule; push detail and edge cases lower or into companion files.
- **When-to-use / when-not**: state both. A skill that never says when *not* to fire mis-activates.
- **Examples + anti-patterns**: show the right call and the common wrong ones.
- **Gold-bar, no slop**: a skill earns its place by changing behavior. If it restates what a capable model already does, don't ship it.

## 3. How to author an MCP SERVER
- **Tools & resources**: model each tool as one clear verb with a **typed input/output schema**. Prefer a few sharp tools over one overloaded one.
- **Structured I/O**: return structured results, not prose blobs — the caller is a program path.
- **Credentials stay out of the server body**: fetch them at call-time from a **secrets broker / secret store**; never bake keys into the server or its config. Keys live in the broker, not the workload.
- **Idempotency & errors**: make writes safe to retry; return typed errors the agent can branch on.
- **Provenance / security audit**: run the server through a **static skill/tool audit** (malicious-code + injection scan) before any agent adopts it.

## 4. Gates and where it ships
Both artifacts clear the same publishing bar — gold-only utility, privacy/secret scan, gated PR, public-first routing. That bar lives in **`skill-curation`**; apply it, don't restate it here.

## When to use / not
- **Use** when classifying a capability/knowledge item, or deciding how to extend an agent.
- **Not** for the line-by-line mechanics of writing a skill (skill-creator / `writing-skills`) or wiring a server (`mcp-builder`) — this picks *which one*, then hands off.

## Anti-patterns
- Building an MCP server for a pure methodology (knowledge-as-a-server).
- A skill that teaches the agent to hand-call an external API (capability-in-prose) instead of an MCP tool.
- One mega-tool with an untyped grab-bag input.
- Credentials embedded in the server instead of fetched from a broker at call-time.
- Adopting a server with no provenance/security audit.
- Publishing either artifact with org traces, or on elegance instead of proven value.
