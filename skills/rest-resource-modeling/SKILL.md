---
name: rest-resource-modeling
description: Use when designing or reviewing a REST/HTTP API: resource names, paths, verbs, status codes, and the contract. Enforces Richardson Maturity Model Level 2 as the deliberate ceiling, plural single-word resource nouns, and schema-first OpenAPI.
---

# REST Resource Modeling

A sharp, opinionated REST taste. This is a **focused complement** to a broader api-and-interface-design guide — it does not re-home that content; it is the specific, enforceable REST opinion set. Cross-reference the broader guide for non-REST interface concerns.

## 1. Richardson Maturity Model Level 2 — the deliberate ceiling
Design to **RMM Level 2** and stop there on purpose:
- **Resources + HTTP verbs + status codes, used honestly.**
- `GET` is safe + cacheable (no side effects). `PUT`/`DELETE` are idempotent. `POST` creates/acts. `PATCH` partial-updates.
- **Honest status codes:** 2xx success (201 + `Location` on create), 4xx client error (400/401/403/404/409/422 meaningfully distinguished), 5xx server error. Never 200-with-an-error-body.
- **Explicitly NOT Level 3 / HATEOAS.** Hypermedia controls are ceremony for most product and internal APIs — cost without payoff. State this as a deliberate choice, not ignorance. (Same anti-ceremony judgment as preferring a plain build over a heavy one when the heavy one doesn't earn its complexity.) Adopt Level 3 only with a concrete reason (e.g. a true public hypermedia API with independent client evolution).

## 2. Plural single-word resource nouns — the forcing function
Resources are **plural, single-word nouns.** Paths nest by ownership:
```
GET  /runs
GET  /runs/{id}
GET  /runs/{id}/jobs
POST /runs/{id}/jobs
```
Hard rules:
- **NEVER verbs in paths.** `POST /createRun` is wrong → `POST /runs`. The verb is the HTTP method.
- **NEVER compound or hyphenated resource names.** `/workflow-runs` is a smell with exactly two cures: (a) nest it under its parent — `/workflows/{id}/runs` — or (b) the domain model needs a **better single word**. Pick one; never ship the hyphen.
- The **single-word constraint is intentional pressure** toward clean resource modeling — if you can't name it in one word, you don't yet understand the resource. (Like a tight module-file rule forcing cohesion: the constraint does the design work.)

## 3. Schema-first OpenAPI is the contract
- **The OpenAPI spec precedes the implementation.** Write the contract, then build to it.
- **All clients consume the one contract** (generated types/clients, not hand-rolled).
- **Contract tests walk every documented operation** — a method/path/status mismatch between spec and server can never ship (this is the ratchet: an API regression is caught in CI, not in prod).

## 4. Supporting conventions (brief, not encyclopedic)
- **Consistent error shape** across every endpoint (e.g. `{error: {code, message, details}}`) — one shape, everywhere.
- **Pagination + filtering**: pick one convention (cursor or limit/offset) and apply it uniformly to all collections.
- **Versioning posture**: decide up front (URI `/v1` vs header); default to a major-version URI prefix for product APIs; never break a shipped contract silently.

## When to use / not
- **Use** for any HTTP/REST API design or review.
- **Not** the right tool for RPC/gRPC/event-stream interfaces — different contracts; see the broader interface-design guide and the typed-RPC guidance.

## Anti-patterns (the smells this skill kills)
- Verbs in paths (`/getRuns`, `/run/create`).
- Hyphenated/compound resources (`/workflow-runs`, `/user-profiles`).
- Singular collection nouns (`/run` for a list).
- 200-with-error-body; wrong/loose status codes.
- Implementation shipped before (or diverging from) the OpenAPI spec.
- Reaching for HATEOAS/Level 3 by default "because it's more RESTful".

## Reference implementation
A telemetry/admin API was built to exactly this spec (schema-first OpenAPI, RMM L2, plural single-word resource paths) — use it as the worked example (consult the sanitized public reference). New services should scaffold schema-first and run a **plural-single-word-noun path lint** as a cheap CI check that ratchets the convention.
