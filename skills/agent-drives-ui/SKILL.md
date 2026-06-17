---
name: agent-drives-ui
description: Use when building an app where an agent operates the UI on the user's behalf through conversation — navigating, filling forms, updating live views, calling frontend tools — with real-time bi-directional state sync. Activates on "conversational app control", "agent drives the front end", "agentic UX", or choosing a protocol to connect an agent backend to a user-facing app.
---

# Agent Drives the UI (AG-UI pattern)

A UX where the **agent operates the application for the user through conversation**: the user says "go to Atlas and show this address" and the agent drives the map + street view; "set the campaign time" and events appear in the live calendar instantly. The agent isn't just chatting beside the app — it **drives** the app, and the UI stays in **real-time sync** both ways.

## Adopt the open protocol — don't invent one
Use **AG-UI (Agent–User Interaction Protocol)** as the standard for this layer rather than a bespoke websocket scheme:
- Open, lightweight, **bi-directional** connection between a user-facing app and any agent backend.
- Streams a **single JSON event sequence** over standard HTTP / **SSE** (optional binary channel).
- Event types: **messages, tool calls, state patches, lifecycle signals.**
- **Shared state**: bi-directional sync of agent ↔ application state (read/write or read-only).
- Built-in **frontend tool calls** (agent invokes UI actions), **human-in-the-loop approvals**, session management.
- Backend-agnostic; TypeScript / Python SDKs.

Source/spec: `github.com/ag-ui-protocol/ag-ui` (CopilotKit). Evaluate `A2UI` if generative/declarative UI is needed (related, different scope).

## Where it sits in the 2026 stack
Layer the protocols, don't conflate them:
- **MCP** — agent ↔ tools/data.
- **A2A** — agent ↔ other agents.
- **AG-UI** — agent runtime ↔ the user-facing app. ← this skill.

## Design rules
1. **Frontend actions are tools.** Model "navigate", "set field", "open view", "create event" as explicit frontend tool calls the agent invokes — not free-text the UI guesses at.
2. **Single source of truth via shared state**, synced both directions: the agent reads what the user sees and writes changes that render instantly (the live-calendar/live-map effect).
3. **Stream, don't block.** Use the event stream (SSE) so the UI reacts in real time as the agent works — partial messages, intermediate state patches, lifecycle signals.
4. **Human-in-the-loop on consequential actions.** Use the protocol's approval events for anything irreversible/outward-facing; the agent proposes, the user confirms.
5. **Degrade safely.** Pair with sim-zero so the UI-driving harness can be exercised without live model calls (see `sim-zero-mode`).

## When to use / not
- **Use** when an agent should operate an app for the user (conversational campaign/CRM/ops management, a "virtual employee" driving tools, conversational dashboards).
- **Don't** reach for it for a plain chat sidebar that never touches app state — that's just a chat box; AG-UI's value is the *driving* + *shared state*.

## Anti-patterns
- A custom event protocol reinventing AG-UI → maintenance burden, no ecosystem.
- Agent mutates UI state out-of-band instead of through synced shared state → the two views drift.
- No approval gate on destructive actions → an agent confidently doing the wrong irreversible thing.
