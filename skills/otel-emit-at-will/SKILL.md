---
name: otel-emit-at-will
description: Use when an agent needs to emit a telemetry value or event mid-reasoning without managing trace context. Route by signal type (metrics first, events, spans only when parentable) so an agent never orphans a span or has to thread context by hand.
---

# otel-emit-at-will

An agent often wants to record something while it works: a value it just computed, an occurrence it noticed. The trap is reaching for a **span** every time, because managing trace context (start, parent, end) from inside a reasoning step is error-prone and **orphans spans**, which pollute the trace and lose the parent relationship. The fix is to route by **signal type** so emitting is safe without threading context by hand. This complements `agent-otel-trajectory` (which emits per-tool-call spans from the harness); this skill is the at-will value/event path.

## Route by signal type

- **Metrics are the primary at-will path.** Counters, gauges, and histograms are **context-free**: they carry no trace or parent, so an agent can emit one at any point, safely, with no setup. Use a metric for any ad-hoc value: a running total, a count, a measured size or duration, a score. This is the default; reach for it first.
- **Events/logs are occurrence markers.** For "X happened" with no value, emit a log record or span event. Attach it to the **active span if one exists**; otherwise emit it standalone. No new span is created just to hold an event.
- **Spans only for bounded operations you can parent.** Create a span only for a clear start-to-end operation, and **auto-parent it to the active trajectory span** via `traceparent`. **Never orphan a span.** If you cannot establish a parent, do not emit a span: record a metric or an event instead. An orphan span is worse than no span.

## Conventions

- **Vendor-neutral OTLP.** Emit via the OTLP protocol to whatever collector is configured; do not hardcode a backend.
- **Semantic conventions.** Use the standard `gen_ai` attributes for agent/model signals so the data is portable and queryable, not bespoke keys.
- **Where the emit path lives.** Emitting is a *write* concern: keep it out of a read-only query/telemetry server. Put it in the runtime, a dedicated emit surface, or a thin wrapper, so the read and write paths stay separate.

## DONE means

The agent records ad-hoc values as **metrics** and occurrences as **events**, reserves spans for parentable bounded operations, and **never emits an orphan span**. Signals reach the OTLP backend correctly attributed (gen_ai semconv), with no hand-threaded trace context in the agent's reasoning.

Sources: OpenTelemetry signal model (metrics vs logs/events vs traces); trace-context (`traceparent`) propagation; OpenTelemetry `gen_ai` semantic conventions.
