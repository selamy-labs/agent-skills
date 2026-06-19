---
name: email-triage
description: Use when building or operating an agent email triage loop that turns recent inbox messages into prioritized, source-linked actions without duplicating project-specific rules across agents.
---

# Email Triage

Email triage is a shared construct, not a per-agent script habit. Keep one
audited workflow for how agents read mail, classify it, log what was seen, and
promote reusable filters back into the shared source of truth.

## Operating Contract

1. Read the canonical mail source with its supported API or local archive, using
   read-only access unless the task explicitly requires state changes.
2. Query a bounded recent window first. Prefer targeted queries by sender,
   subject, list identifier, labels, and age over scanning a full mailbox.
3. Normalize each message into a small record: stable message id, date, sender,
   subject, labels/read state, source mailbox/archive, and a durable link or id.
4. Deduplicate by stable message id before classification.
5. Classify into action categories that match the agent's remit, such as
   failed automation, security alert, review request, team message, customer
   escalation, or informational update.
6. Sort by severity first, then unread/actionable state, then recency.
7. Fetch bodies only for the smallest actionable set, and extract concise
   evidence lines instead of copying whole emails.
8. Append a triage summary to the durable log or wiki with counts, categories,
   message ids or links, and the next action for each actionable item.

## Centralization Rule

- Reusable triage behavior belongs in this skill or a companion shared skill.
- Project-specific sender lists, labels, keywords, and escalation owners belong
  in configuration or memory, not hard-coded in each agent script.
- Long-lived architecture notes belong in the wiki or docs and should point to
  this skill for behavior.
- Config files should contain only a thin pointer to this skill, not a copied
  triage procedure.

When a local script teaches you a better filter or category, promote the generic
rule here and leave only the local values in configuration.

## Evidence Discipline

- Preserve message ids and source locations so another operator can re-check the
  same email.
- Do not paste full private email bodies into logs, issues, or pull requests.
- Redact personal data unless it is required for the task and allowed by the
  destination.
- Log negative runs too: the absence of actionable mail is operational evidence
  when it includes the query window and source.

## Failure Handling

- Authentication failure: report the credential or consent surface that failed,
  then stop before changing mail state.
- Query failure: record the query, status code, and first diagnostic clue.
- Parser failure: keep the raw message id and headers, then quarantine the body
  handling bug for a focused fix.
- Ambiguous relevance: classify as informational and capture why it was not
  escalated.

## Done Standard

An email triage run is done only when the durable log names the source, query
window, counts by category, highest-priority actionable items, and either the
next owner/action or a clear "no actionable mail" result.
