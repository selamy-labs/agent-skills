---
name: polyglot-monorepo-arch
description: Use when choosing build and packaging tooling for a multi-language monorepo. Earn complexity before adopting it — a shared schema contract plus each language's native tooling beats Bazel, Nix, or rules_oci until reproducibility or scale pain is concrete and fleet-wide.
---

# Polyglot Monorepo Architecture

A monorepo spanning several languages tempts teams toward a single heavyweight
build system (Bazel, Nix, rules_oci) for "one way to build everything." That
power has a steep, permanent complexity tax. The principle: **earn the
complexity** — adopt the heavy tool only when a concrete, recurring pain
justifies it, and reach first for a shared contract plus native per-language
tooling.

## Start here (the default that scales surprisingly far)

- **One cross-language contract: a schema, not shared code.** Define the
  interfaces between services in protobuf (or equivalent) and generate per-
  language bindings. This is what actually makes a polyglot repo cohere — every
  language agrees on the wire/API shape — without coupling their build systems.
  (See `protobuf-authoring` for getting that contract right.)
- **Each language uses its own native tooling.** Cargo, uv/pip, npm, go build —
  fast, idiomatic, well-documented, and what contributors already know. Wire them
  together with thin scripts/CI, not a meta-build-system.
- **Plain Dockerfiles for images.** A per-service Dockerfile is legible and
  debuggable; the real complexity of an image build is the dependency/filter
  logic, which a heavyweight OCI builder does not remove.

## When the heavy tooling actually pays off

Adopt Bazel / Nix / rules_oci when the pain is real and recurring, not
anticipated:

- **Bazel** — when incremental/cached builds across many interdependent targets
  are a measured bottleneck, and the team will invest in maintaining the build
  graph. Below that scale it costs more than it saves.
- **Nix** — when you have a concrete reproducibility/"works on my machine"
  failure that pinned native tooling can't solve, and hermetic builds are worth
  the learning curve.
- **rules_oci / standardized OCI builds** — when image builds need to be uniform
  *fleet-wide* and plain Dockerfiles have actually drifted; the value is
  standardization at scale, not a single image.

## How to decide

1. Name the specific pain (slow builds? non-reproducible? fleet drift?) — if you
   can't, you don't need the tool yet.
2. Check the pain is recurring and broad, not a one-off.
3. Confirm the team will own the added machinery; an unmaintained Bazel graph is
   worse than makefiles.
4. Prefer the reversible, incremental adoption (one target/image) over a
   repo-wide migration.

The filter that matters is whether complexity is *earned*. A shared schema
contract buys most of the cohesion a polyglot monorepo needs; the rest is
matching build complexity to demonstrated need.
