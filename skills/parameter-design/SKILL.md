---
name: parameter-design
description: Use when designing or reviewing a function, method, or constructor signature. Make calls unambiguous and misuse-resistant by splitting nullable/optional control switches, removing overload ladders, and keeping defaults behavior-neutral.
---

# Parameter Design

Use this skill when defining or reviewing the signature of a function, method,
or constructor. The signature is the contract: a caller should not be able to
build an invalid or ambiguous call, and the parameter list should document what
the operation needs. Push the cost of clarity onto the design, not onto every
reader.

## Rules

1. **No nullable/optional parameter used as a control-flow switch.** A parameter
   whose *presence or absence changes what the operation does* is two functions
   hiding in one. Split it into distinct, well-named entry points instead of a
   nullable flag that the body branches on.
2. **No chained overloads or grab-bag of optionals.** A long overload ladder, or
   a signature with many optional parameters, is a smell. Give each meaningful
   combination its own clearly-named entry point, or accept one explicit
   options/config object whose required fields are required.
3. **Disciplined defaults.** A default is only for genuinely optional,
   behavior-neutral tuning (a timeout, a buffer size, a page limit). Never
   default a value that changes the *meaning* of the operation. Required things
   stay required — make illegal states unrepresentable.
4. **Self-documenting call sites.** Required parameters are explicit; types carry
   meaning (an enum instead of a boolean or a bare string — see
   `enums-codify-behavior`); avoid positional-boolean traps where `f(x, true,
   false)` tells the reader nothing.

## The smell and the fix

A nullable parameter that gates behavior:

```
# smell: passing a key encrypts; passing nothing does not.
def save(data, key=None):
    if key is None:
        write_plaintext(data)
    else:
        write_encrypted(data, key)

# fix: two operations, each with exactly the inputs it needs.
def save_plaintext(data): ...
def save_encrypted(data, key): ...   # key is required here, not optional
```

An overload ladder / optional grab-bag:

```
# smell: which combination is valid? the reader cannot tell.
def fetch(url, retries=None, timeout=None, cache=None, auth=None, stream=None): ...

# fix: required inputs explicit; optional tuning in one config object.
def fetch(request: Request, options: FetchOptions = FetchOptions()): ...
# Request carries the required url + auth; FetchOptions carries
# behavior-neutral tuning (retries, timeout, cache) with safe defaults.
```

A positional-boolean trap:

```
# smell: render(doc, true, false) — unreadable at the call site.
def render(doc, minify, inline_styles): ...

# fix: an explicit mode and named options.
def render(doc, mode: RenderMode, options: RenderOptions): ...
```

## When to use

- Adding or reviewing any public function, method, or constructor signature.
- A signature is growing optional parameters, or you are about to add the second
  or third overload.
- You catch yourself writing `if param is None:` (or `if not param:`) at the top
  of a function to pick between behaviors.

## When not to / keep it simple

- A genuinely optional, behavior-neutral tuning value (timeout, limit, seed) is
  fine as a defaulted parameter — that is exactly what defaults are for.
- A single optional value that is *absence of data*, not a behavior switch (an
  optional middle name, an optional cursor for pagination), is fine. The test is:
  does presence change *what the operation does*, or only *which data it has*?
- Do not manufacture a config object for a one-or-two-parameter call. Respect the
  complexity budget (`complexity-budgets`).

## Anti-patterns

- A "do everything" function whose first lines branch on which optional arguments
  were supplied.
- Boolean parameters that select behavior (`f(x, recursive=True)`) where the name
  at the call site is invisible — prefer an enum or a separate function.
- Defaulting a destination, a mode, an account, or any other meaning-bearing
  value so that an under-specified call silently does *something*.
- Adding overload number four instead of asking whether these are really
  different operations that deserve different names.

## Review checks

- No parameter's presence/absence switches the operation's behavior; such cases
  are split into named functions.
- Every required input is required in the signature; no meaning-bearing default.
- Defaults are behavior-neutral tuning only.
- Behavioral variants are distinct named entry points or carry an enum, not a
  boolean or string flag (`enums-codify-behavior`).
- The call site reads clearly without the reader opening the definition.

Cross-link: composes with `enums-codify-behavior` (types over flags),
`interface-over-conditionals` (behavioral variants behind one interface), and
`complexity-budgets` (cap parameter count before a signature sprawls).
