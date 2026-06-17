---
name: agents-as-iac-modules
description: Use when onboarding, scaling, decommissioning, or auditing autonomous agents (or any "software employee") in a fleet. Model each agent as a reusable infrastructure-as-code module whose inputs are its facets, so its whole configuration is declarative, reviewable, and reproducible.
---

# Agents as IaC Modules

Treat each autonomous agent — each "software employee" — as a reusable
infrastructure-as-code MODULE, not a hand-assembled one-off. The agent's
configuration becomes a single module instantiation: every meaningful property
is an explicit input, the whole thing lives in version control, and the fleet is
the set of instantiations.

When an agent is a module, the operations that are otherwise manual and
error-prone become declarative:

- **Onboard** a new agent by instantiating the module with new inputs.
- **Offboard** by removing the instantiation; the deletion is the audit trail.
- **Scale** by adjusting an input (replicas, resources) or instantiating again.
- **Audit** by reading the committed inputs — no archaeology across live systems.

This is the higher-level pattern. For the file layout of an individual module
see [[terraform-module-layout]]; for the rule that infrastructure must be
reproducible from source rather than clicked together live see
[[iac-not-ad-hoc]].

## The six facets (the module inputs)

Every agent decomposes into the same facets. Make each one an explicit input so
two agents differ only by their inputs, never by bespoke wiring.

- **Identity** — the account or bot the agent acts AS, and what it is allowed to
  act on. The agent is a first-class principal, not a borrowed human login.
  Onboarding creates the identity; offboarding revokes it.
- **Runtime** — where and how it executes: base image, CPU/memory/limits,
  replicas, restart and lifecycle policy. Sized per agent, declared not tuned by
  hand on a live host.
- **Capabilities** — the tools, skills, and external connectors it may use.
  Capability is granted by input, so least-privilege is reviewable: you can read
  off exactly what an agent can do.
- **Knowledge** — the memory, wiki, or context stores it reads and writes,
  declared as mounts or references. Knowledge persists across restarts and is
  attached, not baked into the runtime.
- **Secrets** — credential MAPPINGS only: a declared reference from a secret
  manager to the agent, injected at runtime. Never the secret value, never
  hand-placed into a running instance. Rotating upstream changes nothing in the
  module.
- **Schedule** — when the agent runs: always-on, cron, or event-triggered. A
  declared input so "when does this run" is answerable from source.

## The module interface

```
module "agent" {
  identity     = ...   # the account/bot it acts as + its grants
  runtime      = ...   # image, resources, replicas, lifecycle
  capabilities = ...   # tools / skills / connectors allowed
  knowledge    = ...   # memory / wiki mounts and references
  secrets      = ...   # declared secret-manager mappings (never values)
  schedule     = ...   # always-on / cron / event-triggered
}
                 │
                 ▼
        one instantiated, running agent
```

The fleet is then just a set of these instantiations side by side in version
control. A reviewer can diff one agent against another and see only the facets
that differ. A new agent is a copy with new inputs, not a fresh investigation of
how agents are built.

## Honest limits

Not every step is fully declarative, and pretending otherwise is the failure
mode. Some inputs require a one-time MANUAL step that the module cannot perform:

- **Interactive auth** — an OAuth consent flow, device login, or browser sign-in
  to mint a token the module then references.
- **App / integration installation** — installing an app into an external
  workspace or org, or approving a connector.
- **External account creation** — registering the underlying identity when the
  provider has no API for it.

Handle these honestly: declare the *reference* (the secret mapping, the installed
app id) in the module, and DOCUMENT the manual step that produces it as a named
prerequisite with a verification check. The goal is "everything that can be
declarative is declarative, and the few steps that can't are written down" — not
a false claim of full automation.

## When to use

- Standing up more than one agent, or expecting to add/remove agents over time.
- You need onboarding/offboarding/scaling/audit to be reproducible and reviewable.
- You want least-privilege and credential custody to be readable from source.

## When not to use

- A single throwaway or experimental agent with no reuse and no audit need —
  the module machinery is overhead. Promote it to a module once a second agent
  appears or it goes anywhere near production.

## Conformance check

Read the committed source — not a live instance — and confirm each facet is an
explicit input:

- Identity is declared and scoped; it is not a shared or human login.
- Runtime (image, resources, lifecycle) is in source, not tuned on a live host.
- Capabilities are enumerated, so least-privilege is reviewable.
- Knowledge stores are declared mounts/references that survive restarts.
- Secrets appear only as manager→agent mappings; no values, no hand-placed files.
- Schedule is an explicit input.
- Every manual prerequisite (interactive auth, app install, account creation) is
  documented with a verification step, and the module references its output.
