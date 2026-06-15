---
name: agent-otel-trajectory
description: Emit OpenTelemetry trajectory spans (tool-call, latency, token usage) as an agent works — vendor-neutral, via otel-cli wrapping shell tool-calls, exported to any OTLP backend.
---

# agent-otel-trajectory

Agent runs are a black box unless they emit telemetry. To debug, optimize cost, and grade the journey (not just the answer), an agent should emit OpenTelemetry **trajectory spans** as it works: one span per tool-call / iteration, with latency, token usage, and outcome. Backend-only or LLM-API-only metrics miss the agent's actual decision path.

## The landscape (survey)
- **`equinix-labs/otel-cli`** — the mature, vendor-neutral CLI for emitting OTLP spans from shell: `otel-cli exec --name "<step>" -- <command>` wraps any command in a span (attributes, events, span kinds, context propagation, background spans). **Best fit for EMITTING** trajectory telemetry from shell-driven agents/harnesses.
- **`traceloop/opentelemetry-mcp-server`** — an MCP server for *querying/analyzing* existing traces (Jaeger/Tempo/Traceloop). Use it for an agent that *debugs via traces*, NOT for emitting its own — a different job.
- **OTel GenAI/MCP semantic conventions** (`opentelemetry.io/docs/specs/semconv/gen-ai/`) — use the standard attribute names so spans are portable.

## Recommendation
Don't make the agent call a tool to emit its own spans (circular, unreliable). Instrument the **harness/loop** (the thing that runs tool-calls) to wrap each tool-call in an otel-cli span. No MCP-server dependency; works anywhere a shell does.

## The pattern
1. **One root span per agent run**, child spans per iteration/tool-call:
   ```
   otel-cli exec --service "<agent>" --name "tool:<name>" \
     --attrs "gen_ai.operation.name=<tool>,gen_ai.request.model=<model>,gen_ai.usage.input_tokens=<n>,gen_ai.usage.output_tokens=<n>" \
     -- <the tool command>
   ```
2. **Attributes that matter** (use semconv): `gen_ai.request.model`, `gen_ai.usage.input_tokens`/`output_tokens`, `gen_ai.operation.name`, the tool name, the task/run id, success/error. Latency is the span duration (automatic).
3. **Context propagation:** propagate the traceparent down so child tool-calls nest under the run (otel-cli `--tp-print`/env propagation).
4. **Export vendor-neutral OTLP** → the environment's backend (e.g. Cloud Trace on GCP) via the OTLP endpoint env (`OTEL_EXPORTER_OTLP_ENDPOINT`). Never hardcode a vendor.
5. **Emit on failure too** — error spans with the failure attribute are the most valuable (where the trajectory broke).

## DONE means
An agent run produces a trace in the backend with one span per tool-call showing latency + token usage + outcome, nested under a run span, using gen_ai semconv attributes — so a failure or a cost spike is visible in the trace, not inferred. Verified by opening the trace, not by "instrumented".
