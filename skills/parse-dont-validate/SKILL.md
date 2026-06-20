---
name: parse-dont-validate
description: Use at a boundary that takes untrusted input — API, config, user data, a queue message. Parse it into a typed value that makes invalid states unrepresentable, instead of validating and passing the raw data onward for the core to re-check or blindly trust.
---

# Parse, Don't Validate

A validator answers a yes/no question and throws the answer away: `is_valid(x)`
returns `true`, then the *same raw `x`* flows onward and every downstream
function has to wonder whether it was checked. A parser answers the question and
*keeps the proof* — it turns the raw input into a typed value that, by
construction, can only hold valid data. After parsing, "is this valid?" is no
longer a question anyone downstream can even ask, because invalid data can't be
represented.

## Validate-and-forward vs parse-into-a-type

**Validate-and-forward (the boundary bug waiting to happen):**
```python
def handle(payload: dict):
    if not is_valid_order(payload):      # check…
        raise BadRequest
    process(payload)                     # …then hand the raw dict onward

def process(payload: dict):
    qty = int(payload["qty"])            # re-parse, re-trust, deep in the core
    ...
```
The check's knowledge is lost at the door. `process` (and everything it calls)
receives an untyped `dict` and must re-extract, re-coerce, and re-trust — or
quietly assume. That assumption is where the boundary bug lives.

**Parse-into-a-type (the check becomes a value):**
```python
@dataclass(frozen=True)
class Order:                              # a type that cannot hold invalid data
    symbol: Symbol
    qty: PositiveInt

def parse_order(payload: dict) -> Order:  # parse at the edge, once
    return Order(symbol=Symbol(payload["symbol"]),
                 qty=PositiveInt(payload["qty"]))

def handle(payload: dict):
    process(parse_order(payload))         # core only ever sees Order

def process(order: Order):                # total: no re-checking, no coercion
    ...
```
The parse happens once, at the boundary. The core is *total* — it can't be
handed a malformed order because one can't be constructed.

## Make illegal states unrepresentable

Reach for a type that can't hold bad data instead of a primitive plus a rule you
have to remember to run: `PositiveInt` not `int`-that-must-be-positive,
`NonEmptyList` not "list (callers: don't hand it an empty one)", an enum or tagged union
not a string you re-check with `if status in {...}`. The compiler/constructor
then enforces at every call site what a validator only enforced where you
remembered to call it.

## Push the parse to the edge, keep the core total

Validate **once**, at the boundary, into a typed contract; let the interior
trust its types. The smell that you've inverted this: the same property
re-checked in three places, `Optional`/`None` guards scattered deep in the call
tree, or "stringly-typed" `dict`/`str` data flowing far inward and being
coerced late. Each is a place an unparsed value reached somewhere that assumed
it was clean.

## Where it fits

The typed values you parse into are often enums or tagged unions (codify
behavior in the type) and feed misuse-resistant signatures (parameter design):
parsing produces the safe types; those skills make the types carry behavior and
make the functions that take them hard to call wrong. Parse at the boundary →
the rest of the system is total.
