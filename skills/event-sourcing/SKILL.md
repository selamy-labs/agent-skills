---
name: event-sourcing
description: Use when designing a system where state-change history matters — audit, replay, temporal queries, debugging, or telemetry provenance. Record every change as an immutable domain event and derive state from the log; guard external side effects during replay.
---

# Event Sourcing

Most systems store only current state, so every update destroys the history of how it got there. Event sourcing inverts that: **capture every change to application state as an event object, append it to an immutable log, and make that log the system of record.** Current state becomes a derivation — you can throw it away and rebuild it by replaying the events. When history, auditability, or "what did the system believe at time T?" matters, store the events, not just the outcome.

## Building blocks

- **Command** — a request to change state. It may be rejected; it is not a fact.
- **Domain event** — an immutable, past-tense fact recorded once a change happens (`order-placed`, `quota-exceeded`). Events are the *only* way state changes enter the system: process an update by creating an event object and running it through the same processing logic on live handling and on replay. Name events in domain terms, carry enough data to reprocess them, and timestamp both occurrence and recording.
- **Projection (working copy)** — any state derived from the log: the current-state view, a report table, a cache. Projections are disposable and rebuildable; consumers read projections and never need to understand the raw log.

## What the log buys you

- **Complete rebuild** — discard corrupted or redesigned state and re-derive it from the log.
- **Temporal query** — determine the state as of any past moment by replaying events up to it.
- **Replay debugging** — reproduce an incident by replaying the exact event sequence into a test copy; fix the handler, replay again.
- **Audit** — the log *is* the audit trail, not a bolted-on shadow of it.

## Rules that keep it sound

- **Events are append-only facts.** Never edit or delete one. Correct a mistake with a compensating event, the way accountants correct a ledger.
- **Guard external side effects during replay.** Replaying `payment-authorized` must not charge the card twice or resend the email. Route all external interactions through gateways that know replay mode and suppress outbound calls while reprocessing. If a side effect cannot be guarded, that flow is a poor fit for replay — say so explicitly.
- **Version your event schemas.** The log is forever; handlers must read old event shapes. Without schema/versioning discipline, replay rots.
- **Don't confuse it with messaging.** Event sourcing is a *persistence* pattern. It is not asynchronous messaging, and a mere event *notification* ("something changed, come ask me") is not event sourcing — the test is whether the event log alone can rebuild state.

## Telemetry and instrumentation

Counters and gauges tell you the latest value; an event log tells you how it got there. When instrumenting a system where provenance matters — quota consumption, budget spend, fleet membership, config drift — emit the state *changes* as events with enough data to re-derive the running state, and treat dashboards as projections. Then "why is this number wrong?" becomes a replay, not archaeology.

## When NOT to use it

- Simple CRUD where history has no consumer.
- High write volume with no replay, audit, or temporal-query need — you pay log-forever costs for nothing.
- External side effects that cannot be gated behind replay-aware gateways.
- Teams without schema-versioning discipline; an unversionable log is a liability.

Prefer plain state storage in those cases; you can still log domain events for analytics without making the log the system of record.

## DONE means

State changes are recorded as immutable, replayable domain events; current state is demonstrably rebuildable from the log alone; replay is side-effect-safe via gateways or replay-mode guards; and event schemas are versioned. Verified by actually rebuilding a projection from the log, not by "we write events".

Sources: Martin Fowler — Event Sourcing (https://martinfowler.com/eaaDev/EventSourcing.html), Domain Event (https://martinfowler.com/eaaDev/DomainEvent.html), What do you mean by "Event-Driven"? (https://martinfowler.com/articles/201701-event-driven.html), Event Collaboration (https://martinfowler.com/eaaDev/EventCollaboration.html).
