---
name: product-feedback-loop
description: Use when turning stakeholder, reviewer, customer, beta, analytics, or interview feedback into product decisions, tracked work, evals, experiments, or an explicit decision to gather more evidence.
---

# Product Feedback Loop

Use this when feedback should change what gets built, measured, or decided.
Feedback can come from a stakeholder review, product critique, customer report,
beta cohort, support thread, user interview, analytics signal, or sales proxy.

The loop is not "do whatever the latest feedback says." It converts signals
into decisions with source lineage, confidence, priority, and verification.

## Classify The Signal

For each feedback item, name:

- **Source:** reviewer, customer, beta cohort, analytics, support, interview,
  sales proxy, or other.
- **Evidence level:** direct observation, reproduced behavior, measured trend,
  repeated report, single anecdote, or secondhand proxy.
- **Feedback type:** bug, missing capability, UX friction, product strategy,
  unclear expectation, instrumentation gap, or already-handled item.
- **Affected user path:** the workflow, segment, or job-to-be-done involved.
- **Confidence:** high, medium, or low, with the reason.

Keep source lineage explicit. Use [[grounded-generation]] when summarizing or
combining feedback from external facts.

## The Loop

1. **Capture without obeying.** Restate the feedback faithfully, but separate
   the user's words from the product decision.
2. **Cluster by theme.** Group repeated reports, related UX friction, and
   shared failure modes. Do not let one loud anecdote become the whole roadmap.
3. **Map to current truth.** Check the current product, docs, issue tracker,
   PRD, metrics, or code before assuming the feedback is still accurate.
4. **Choose a response.** For each cluster, pick one:
   - fix a reproduced bug;
   - improve a confusing flow;
   - add or adjust instrumentation;
   - run an experiment;
   - update the product spec or issue;
   - ask for a decision;
   - gather more evidence; or
   - explicitly defer.
5. **Create the smallest tracked unit.** Convert chosen responses into issues,
   PRD changes, evals, experiments, or implementation slices. Use
   [[small-focused-changes]] and [[agentic-coding-loop]] for code follow-up.
6. **Add a measurable check.** Define the acceptance test, metric, event, rubric,
   or user-path verification that will show the response worked.
7. **Report decisions and uncertainty.** Tell stakeholders what is changing,
   what is not changing, what needs a decision, and what evidence is missing.

## Prioritization

Prioritize feedback higher when it is:

- frequent across independent sources;
- severe for a core workflow;
- blocking activation, retention, revenue, safety, or trust;
- cheap to verify and fix;
- aligned with the current product goal; or
- supported by both qualitative and quantitative evidence.

Prioritize lower when it is:

- a one-off preference with no supporting pattern;
- outside the target segment;
- contradicted by current metrics or source-of-record checks;
- a feature request that hides an unvalidated product decision; or
- costly relative to the confidence and impact.

## Stop Conditions

Stop with tracked work when:

- the feedback cluster has a clear response;
- the response has an owner, artifact, or implementation path;
- success can be measured or verified; and
- the decision is consistent with the current product goal.

Stop with a decision request when:

- two valid product directions conflict;
- the feedback implies a target-user, pricing, launch, privacy, or safety
  decision;
- the confidence is too low for implementation but high enough to investigate;
  or
- the requested change would invalidate existing commitments.

Stop with no action when:

- the feedback is stale, already addressed, out of segment, or unsupported;
- the right response is to monitor for recurrence; or
- the cost of action exceeds the expected value.

## Output Shape

```text
Feedback cluster: <theme>
Source lineage: <sources and confidence>
Decision: <fix, improve, measure, experiment, ask, gather, defer>
Tracked artifact: <issue, PRD, eval, metric, experiment, PR, or none>
Verification: <how we will know the response worked>
Open question: <none or explicit decision needed>
```

## Anti-Patterns

- Treating all feedback as equally important.
- Shipping a change from a single anecdote without naming confidence.
- Losing the original source while summarizing.
- Turning product decisions into implementation tickets without owner sign-off.
- Letting analytics override direct user pain without investigating the gap.
- Closing the loop with a reply but no tracked artifact or measured outcome.
