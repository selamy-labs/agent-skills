---
name: adaptive-estimation
description: Use when tracking a slowly-varying latent quantity from noisy measurements with quantifiable uncertainty — a drifting metric, capacity, latency trend, or confidence. Prefer a Kalman-style adaptive estimator over fixed moving averages or static thresholds.
---

# Adaptive Estimation

When you are tracking a quantity you cannot observe directly — a true value that
drifts slowly while your measurements are noisy — a fixed-window moving average
and a static anomaly threshold are both crude. The window is either too short
(jittery) or too long (laggy), and a static threshold cannot tell a real shift
from ordinary noise. A **Kalman-style recursive estimator** solves both: it
auto-tunes how much to trust each new measurement, and its prediction error is a
built-in surprise detector.

## When this applies (scope it tightly)

Use this only when the preconditions hold; otherwise it is over-engineering:

- there is a **single latent quantity** (or a small state vector) that evolves
  **slowly and roughly linearly** between steps
- each measurement is **noisy** but you can put a **number on the uncertainty**
  of both the measurement and the process drift (variances, even rough ones)
- the noise is **approximately Gaussian / unimodal** — no heavy multi-modal
  structure
- you want a continuously-updated **estimate** of the current value plus a
  measure of how confident you are in it

This is an **estimation layer**: it smooths and tracks a value and flags
surprises. It is **not** a forecaster of structural regime change, **not** a way
to manufacture predictive signal where none exists, and **not** a substitute for
a real model of the system. It cleans and tracks; it does not divine.

## The recursive update (1-D scalar form)

Maintain an estimate `x` and its variance `P`. Each step:

**Predict** (let the model drift; uncertainty grows by process noise `Q`):
- `x⁻ = x`  (or `x = F·x` if there is known drift dynamics)
- `P⁻ = P + Q`

**Update** with a new measurement `z` of measurement-noise variance `R`:
- innovation (residual): `y = z − x⁻`
- gain: `K = P⁻ / (P⁻ + R)`
- new estimate: `x = x⁻ + K·y`
- new variance: `P = (1 − K)·P⁻`

`K` lives in `[0, 1]` and **auto-tunes trust**: when measurements are noisy
relative to the model (`R ≫ P⁻`), `K → 0` and the estimate barely moves; when
the model is uncertain relative to a clean measurement (`P⁻ ≫ R`), `K → 1` and
the estimate snaps to the observation. You set `Q` and `R`; the gain adapts on
its own. The vector form generalizes this with matrices `F, Q, H, R` and the
same predict/update structure.

## The innovation is a free anomaly detector

The residual `y` has expected variance `S = P⁻ + R`. A **normalized innovation**
`y / √S` is, under the model's assumptions, roughly unit-variance. So:

- a normalized innovation beyond a few standard deviations = a **statistically
  surprising** measurement — a principled anomaly flag, adaptive to current
  uncertainty rather than a hand-set fixed threshold
- persistently biased innovations (a run of same-sign residuals) means the model
  is wrong — your `Q`/`R` are mistuned or the dynamics are not what you assumed

You get smoothing and anomaly detection from the **same** recursion, for free.

## When to escalate (and when not to)

- Genuinely **non-linear** dynamics or measurement function → Extended (EKF) or
  Unscented (UKF) Kalman filter. Reach for these only when a real non-linearity
  forces it — they cost complexity and tuning.
- **Multi-modal / non-Gaussian** state → particle filter. Heavier still.
- Do **not** jump to EKF/UKF/particle filters by default. The linear scalar
  filter above handles a surprising share of "track a drifting noisy number"
  problems; escalate only on evidence (biased innovations, known non-linearity).

## Anti-patterns

- a fixed-window moving average that is simultaneously too laggy and too jittery
  because one window cannot serve both
- a static anomaly threshold that ignores how confident you currently are
- treating this estimation layer as if it predicts future structural change
- inventing `Q`/`R` and never checking the innovations to see if the model holds
- reaching for an EKF/UKF/particle filter before establishing that the simple
  linear-Gaussian filter is actually insufficient

## Done means

A slowly-varying latent value is tracked with a recursive estimator whose gain
auto-tunes trust between model and measurement using stated `Q`/`R`; the
normalized innovation is used as an adaptive surprise/anomaly signal; the model's
fit is sanity-checked via the innovation sequence; and any escalation to a
non-linear filter is justified by evidence, not chosen by default.
