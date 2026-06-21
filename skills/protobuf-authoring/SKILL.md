---
name: protobuf-authoring
description: Use when writing or reviewing a .proto schema. Apply consistent naming/style and — the part that actually bites — the wire- and consumer-compatibility rules, so the schema reads cleanly and can evolve without breaking deployed readers. Gate it with buf lint plus breaking-change detection.
---

# Protobuf Authoring

Two things make a proto good: **consistent style** (so generated code is
predictable across languages) and **never breaking the wire/consumer contract**
(the mistakes that are expensive and effectively irreversible once a schema
ships). Style is a checklist; compatibility is the real expert knowledge — get
it wrong and you corrupt data in already-deployed readers.

## Style checklist

- **File**: `lower_snake_case.proto`, one top-level concept; `syntax = "proto3";`
  first line.
- **Package**: lowercase dotted, versioned — `acme.orders.v1`. The `v1` is not
  optional; it's how you ship a v2 later without a collision.
- **Message / enum / service / RPC names**: `PascalCase`.
- **Field names**: `lower_snake_case`. Repeated fields get plural names.
- **Enum values**: `UPPER_SNAKE_CASE`, **prefixed** with the enum name to avoid
  C++ scoping collisions: `enum Side { SIDE_UNSPECIFIED = 0; SIDE_BUY = 1; }`.
- **The zero value is always `*_UNSPECIFIED = 0`** — see below; this is a
  compatibility rule, not just style.
- Indent 2 spaces; keep one request/response message per RPC
  (`GetFooRequest`/`GetFooResponse`) so each can evolve independently.

## Compatibility rules (the ones that bite)

These protect already-deployed readers. Treat every shipped field number as
permanent.

- **Never change or reuse a field number.** The number is the wire identity. Reusing
  a freed number makes old readers parse new data into the wrong field — silent
  corruption.
- **When you remove a field, `reserve` its number AND its name** so neither can
  be reused by accident:
  ```proto
  message Order {
    reserved 4, 7 to 9;
    reserved "old_status";
  }
  ```
- **Never change a field's type** unless the new type is wire-compatible (the
  list is narrow: e.g. `int32`/`int64`/`uint32`/`uint64`/`bool` interconvert).
  When unsure, add a new field instead.
- **Enums are append-only** and must have a `0` = `UNSPECIFIED` value. Proto3 has
  no field presence for scalars, so `0` is what an old reader sees for an unknown
  new enum value — it must mean "unset/unknown," never a real case.
- **Don't rename fields** if any consumer uses the JSON mapping (the field name is
  the JSON key); the number is safe but the name is part of that contract.
- **Avoid `required`** (proto2) entirely — you can never safely make a required
  field optional later. In proto3 everything is optional by design; use
  `optional` only when you genuinely need presence on a scalar.
- **Adding** a field or enum value is the safe move — defer to it whenever you'd
  otherwise mutate.

## Good vs bad

**Bad** — reuses a number, no UNSPECIFIED, no reservation:
```proto
enum Side { BUY = 0; SELL = 1; }        // 0 is a real case; collides unprefixed
message Order {
  string id = 1;
  int64 qty = 2;
  // (price used to be field 3, deleted — number now free to be reused) ☠
}
```

**Good** — prefixed enum with UNSPECIFIED, reserved removed field:
```proto
enum Side { SIDE_UNSPECIFIED = 0; SIDE_BUY = 1; SIDE_SELL = 2; }
message Order {
  reserved 3; reserved "price";          // price removed, locked forever
  string id = 1;
  int64 qty = 2;
  Side side = 4;                          // new field = new number
}
```

## Lint gate

Enforce both halves in CI with [`buf`](https://buf.build): style on every
change, breaking-change detection against the published baseline.

```yaml
# buf.yaml
version: v2
lint:
  use: [STANDARD]            # naming, enum-zero-value, package-version, etc.
breaking:
  use: [WIRE_JSON]           # blocks number reuse, type changes, deletions w/o reserve
```

```yaml
# CI
- run: buf lint
- run: buf breaking --against '.git#branch=main'   # fail the PR on a breaking change
```

`buf breaking` is the safety net the human reviewer can't reliably be: it
mechanically catches the number-reuse and type-change mistakes above before they
ship.

---

_Synthesized from the public [protobuf.dev](https://protobuf.dev) style guide and API/proto-best-practices docs._
