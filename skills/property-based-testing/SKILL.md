---
name: property-based-testing
description: Use when example tests can't cover the input space — parsers, serializers, encoders, money math, state machines, anything with a roundtrip or invariant. Assert properties that hold for all inputs, let the framework generate cases and shrink failures to a minimal counterexample.
---

# Property-Based Testing

Example tests check the handful of inputs you thought of. Property-based tests
check a property against thousands the framework generates — and when one fails,
it *shrinks* the failure to the smallest input that still breaks, handing you a
minimal repro instead of a haystack. Reach for it where the input space is too
big to enumerate and a clear invariant exists.

## Where it earns its keep

- **Roundtrips**: `decode(encode(x)) == x` for all `x` — serializers, parsers,
  protobuf/JSON codecs, compression.
- **Invariants**: a sorted list is sorted and a permutation of the input; a
  balance never goes negative; a state machine never reaches an illegal state.
- **Equivalence / oracle**: the fast/new implementation agrees with the
  slow/reference one for all inputs (the parallel-run check in a rewrite).
- **Idempotence / commutativity**: `f(f(x)) == f(x)`; `apply(a,b) == apply(b,a)`.

## Finding the property

The skill is naming the property, not writing the generator. Ask: what must be
true *regardless* of the input? Common shapes — "there and back" (roundtrip),
"different path, same result" (oracle/commutativity), "some things never change"
(invariant), "doing it twice = doing it once" (idempotence). If you can't state
one, the code may not have a crisp contract yet — that's itself a finding.

## Shape of a test

```python
from hypothesis import given, strategies as st

@given(st.binary())
def test_roundtrip(payload):
    assert decode(encode(payload)) == payload     # holds for ALL payloads
```

## Discipline

- **Assert behavior, not the implementation** — a property restating the code
  proves nothing.
- **Save the shrunk counterexample as a regression example test** — property
  tests find the bug; a pinned example keeps it dead (pairs with
  regression-ratchet).
- **Constrain generators to the real domain** (valid ranges, encodings) so
  failures are real bugs, not "we never accept that input anyway."
- **Don't replace example tests** — keep readable examples for the common path;
  add properties for the space you can't enumerate. They're complementary.
