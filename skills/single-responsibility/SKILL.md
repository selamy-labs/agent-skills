---
name: single-responsibility
description: Use when a class, function, module, or file is hard to reason about. A unit you cannot describe in one sentence holds too many responsibilities; decompose it into single-responsibility units that collaborate through clear interfaces.
---

# Single Responsibility

A unit of code — a class, function, module, or file — should have **one
responsibility**: one reason to change, one job you can state in a single
sentence. When a unit is **hard to reason about**, that difficulty is the
signal that it has accreted several responsibilities. The fix is not to comment
it or split it arbitrarily; it is to **separate the concerns** into distinct
single-responsibility units that collaborate through clear interfaces.

This is the long-established Single Responsibility Principle / separation of
concerns, applied as an enforceable code-design habit.

## The bar for a good unit

After decomposing, each unit should satisfy all three:

1. **One sentence.** You can say what it does, how to use it, and what it
   depends on — each in one sentence. If the "what it does" sentence needs an
   "and", you probably have two units.
2. **Understand from the outside.** A caller can use it correctly from its
   name and signature, without reading its internals.
3. **Change the inside safely.** You can change how it works internally without
   breaking any consumer, because consumers only depend on its interface.

## Detection heuristics (when to decompose)

Concrete signals that a unit owns too much:

- **More than one reason to change.** A change to formatting *and* a change to
  the data source *and* a change to retry policy all land in the same unit.
- **Too big to hold in your head.** You scroll, or lose the thread, to
  understand one path through it.
- **Too many parameters or too much state.** A long parameter list or a pile of
  fields that don't all relate to each other (see `parameter-design`,
  `complexity-budgets`).
- **High branching / nesting.** Cyclomatic or cognitive complexity over the
  budget; deeply nested control flow (see `complexity-budgets`,
  `early-return-over-else`).
- **A name that needs "and", or hides behind "Manager"/"Util"/"Helper"/
  "Service"/"Processor".** A vague catch-all name is a unit with no single
  answer to "what is it responsible for?"
- **Tests need elaborate setup.** If exercising one behavior forces you to
  stand up five unrelated collaborators, the unit is doing five things.

One signal is a hint; several together mean decompose now.

## The move: decompose, then delegate

1. **Name the responsibilities.** List the distinct jobs the unit is doing —
   each becomes a candidate unit. The names should be answerable in one
   sentence; if you can't name a clean job, the seam is wrong.
2. **Extract each into its own unit** with a focused interface — the smallest
   surface a caller needs, hiding the internals behind it.
3. **Turn the original into a coordinator that delegates.** It wires the pieces
   together and calls them; it no longer does the work inline. A thin
   coordinator with a clear story beats a fat unit that does everything.
4. **Keep behavior unchanged.** Decompose as a behavior-preserving refactor
   first (tests stay green), then make any behavior change separately (see
   `small-focused-changes`).

## Before / after

A function that validates, transforms, persists, and notifies — four reasons to
change in one body:

```python
# before: one function, four responsibilities, hard to test in isolation
def handle_signup(raw):
    if "@" not in raw["email"] or len(raw["password"]) < 8:
        raise ValueError("invalid")
    user = {"email": raw["email"].lower(), "hash": bcrypt(raw["password"])}
    db.execute("INSERT INTO users ...", user)
    smtp.send(user["email"], render_welcome(user))
    return user

# after: each unit has one job; handle_signup is a coordinator that delegates
def handle_signup(raw, validator, store, mailer):
    fields = validator.validate(raw)      # validation: one reason to change
    user = store.create(fields)           # persistence: one reason to change
    mailer.send_welcome(user)             # notification: one reason to change
    return user
```

Each collaborator (`validator`, `store`, `mailer`) is now usable and testable
on its own, swappable behind its interface, and `handle_signup` reads as the
story of a signup.

A class that has grown an "and":

```typescript
// before: "manages orders AND prices them AND talks to the gateway"
class OrderManager {
  price(order: Order): Money { /* tax + discount rules */ }
  charge(order: Order): Receipt { /* payment-gateway calls */ }
  save(order: Order): void { /* persistence */ }
}

// after: one responsibility each; a coordinator composes them
class Pricer     { price(order: Order): Money { /* ... */ } }
class Payments   { charge(order: Order, total: Money): Receipt { /* ... */ } }
class OrderStore { save(order: Order): void { /* ... */ } }

class Checkout {
  constructor(
    private readonly pricer: Pricer,
    private readonly payments: Payments,
    private readonly orders: OrderStore,
  ) {}
  place(order: Order): Receipt {
    const total = this.pricer.price(order);
    const receipt = this.payments.charge(order, total);
    this.orders.save(order);
    return receipt;
  }
}
```

## When NOT to (keep it simple)

- **A small unit that already reads in one sentence** needs no splitting.
  Decomposition is for units that are hard to reason about, not a quota.
- **Don't shatter one cohesive job into fragments** that only make sense
  together — that creates artificial coupling and worse locality, and is its
  own complexity-budget violation.
- **Don't manufacture interfaces with a single implementation** that will never
  vary just to look decomposed; introduce the seam when a real responsibility
  or variant exists (`interface-over-conditionals` covers the behavioral-variant
  case specifically).
- The target is the **smallest coherent unit**, not the smallest possible one.

## Review checks

- Each unit's responsibility is stateable in one sentence with no "and".
- Callers use each unit from its interface without reading its internals.
- Internals can change without breaking consumers.
- The coordinator delegates; it does not inline the work it orchestrates.
- The decomposition was behavior-preserving; any behavior change shipped
  separately.

Cross-link: composes with `small-focused-changes` (decompose the *change*,
this decomposes the *unit*), `complexity-budgets` (enforceable size/branching
signals), `parameter-design` (too many params signals too many jobs),
`early-return-over-else` (flatten before you split), `interface-over-conditionals`
and `map-dispatch-over-conditionals` (decompose behavioral variation behind an
interface), and `technical-integrity` (decompose for truth, not to hit a quota).
