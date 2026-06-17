---
name: credential-proxy
description: Use when a workload (agent, sandbox, container, CI job) must call an authenticated upstream (LLM/provider/cloud API) but should not hold the credential itself. Keep the secret in a proxy that injects it, so a compromised workload cannot exfiltrate the key.
---

# Credential Proxy

Keep upstream credentials OUT of the workload. Put a small reverse proxy between
the workload and the upstream API; the proxy holds the secret and injects it. The
workload authenticates to the proxy (cheap, internal) and never sees the upstream
key.

## When to use

- An agent / sandbox / container / CI job needs an LLM or provider API key.
- The credential is high-value: painful to rotate, billing-attached, or broadly scoped.
- You want one place to rotate, audit, rate-limit, or kill-switch upstream access.

## The shape

```
workload  ──▶  proxy (holds the secret)  ──▶  upstream API
   no key      injects Authorization          Anthropic / OpenAI / cloud
```

- The workload calls `proxy/<provider>/...` with **no** upstream key.
- The proxy injects the `Authorization` / API-key header from a secret store.
- The proxy streams the response back and strips anything sensitive.

## Why it is worth it

- **Blast radius:** a compromised workload cannot exfiltrate the key — it never had it.
- **Central rotation:** rotate in one place; workloads are unaffected.
- **Control point:** the proxy is the natural choke point for logging, rate limits,
  quotas, and a kill switch.

## Build requirements

- **Secret source:** read the key from a secret store / systemd credentials / a k8s
  secret mounted ONLY to the proxy — never an env var in the workload.
- **Inject on ingress, strip on egress:** add the upstream auth header; drop any
  client-supplied auth; never reflect the key back.
- **Authenticate the workload→proxy hop:** per-client token or mTLS. A proxy any
  workload on the network can use with no auth is just credential laundering — that
  is the failure mode to avoid.
- **Limits:** cap request and response body size; enforce per-client rate limits/quotas.
- **Streaming:** flush SSE/chunked responses promptly; do not buffer the whole stream.
- **Hygiene:** never log the secret; TLS on both hops.

## Verify (real artifact)

- The workload has NO upstream key in its env or mounted secrets (inspect it — absent).
- A real request through the proxy still authenticates upstream and returns a response.
- Rotating/killing the secret at the proxy immediately affects all workloads (one place).
- The proxy rejects an unauthenticated client.
