---
name: pull-request-writing
description: Use when opening or updating a pull request title and description so a reviewer who has not read the agent conversation can understand and verify the change.
---

# Pull Request Writing

Write the PR for a reviewer who has not read the agent conversation. The title and body must carry the whole story: what behavior changes, why, and what evidence backs it.

## The recipe

1. **Title names the concrete behavior and component.** Follow this repository's title convention (check CI: conventional-commit title, allowed type, scope rules). Name what changes and where: the behavior plus the component or area. Leave out worker IDs, hashes, acceptance labels, vague verbs ("fix", "update"), and praise.
2. **Lead the body with problem then behavior.** First paragraph: the trigger or failure in plain words. Second: what now happens instead. Add one concrete before/after or trigger example when the change is hard to picture without it. If the repository ships a PR template, fill its sections rather than replacing them; fit the recipe into the template's shape, and keep any required checklist honest rather than ticking it by default.
3. **Describe the implementation briefly, scaled to the change.** A small change gets one or two sentences plus rationale. A larger change gets one short paragraph per part: what changed, what it connects to, and why this shape was chosen.
4. **State the validation honestly.** Name the checks actually run (build, tests, linters, repo-specific scanners), their scope, and their outcome. Name material limits, risks, and dependencies too. Keep delivered source separate from deployed proof: a merged change is not the same as a verified running system.
5. **Keep links and receipts compact.** Put issue and spec links plus required review evidence in one short validation section. Link to operational output (logs, run pages, dashboards) instead of pasting it inline.
6. **Rewrite the title and body when the scope changes.** A title that fit the first diff but not the final one misleads every later reader.
7. **Describe the entire final diff, not the last fix.** Review the whole PR against its base including every inherited commit. A correction-only diff is not the PR scope: the title and body must name all material final changes, the hosted validation actually run on the final head, and remaining limits — never only the last one-line fix. A narrower follow-up description that omits inherited scope contradicts this rule; keep every inherited change in scope.
8. **Keep lasting norms in the canonical spec package.** Requirements, behavior, architecture decisions, and normative tool or quality rationale belong in the existing spec structure (spec, plan, contracts, tasks), updated in place. Do not publish standalone rationale reports, selection notes, parallel design documents, or status histories beside it. This is not authority for blanket deletion: preserve legitimate user, API, and reference docs. Before publication, verify each new normative document belongs to the canonical structure and its links resolve. Attempt details, run pages, and review receipts stay linked from the PR, not duplicated into lasting docs. Use generic examples only; keep private project names, paths, and receipts out of public prose.

## Mechanics

- Write readable Markdown with real newlines. Build API calls with structured arguments or a body file (`gh --body-file`); never interpolate free text into a shell string.
- After saving, read back the rendered title and body and confirm they show what was intended.
- Review the description alongside the code before merge, the same as the diff itself.

## Short example

Title: `fix(auth): reject expired session tokens at the API boundary`

Body:

```markdown
Expired sessions returned a generic 500 from deep in the request handler.

They now fail fast with 401 at the API boundary, so clients refresh the
session instead of retrying a doomed request.

Changed `session.py` to validate expiry before dispatch; expiry checks live
in one place so future handlers inherit the behavior.

## Validation

- `pytest tests/test_session.py -q`: 24 passed.
- `ruff check src/`: clean.
- Limit: covers API sessions only; web-cookie expiry is unchanged and tracked
  in #412.
```

## Complex example

Title: `feat(sync): retry failed uploads with backoff and keep local order`

Body:

```markdown
Uploads failed permanently on the first transient network error, and retries
— when added by hand — reordered the local queue, so users saw items arrive
out of order after a flaky connection.

Uploads now retry up to 5 times with exponential backoff, and the queue keeps
its original order across retries. A retry that exhausts its budget surfaces
as a failed item with its error attached instead of blocking the queue.

The retry loop lives in the upload worker; ordering is preserved by holding
each item's slot until it resolves. Backoff caps the extra load; the
failed-item state keeps one bad upload from stalling the rest.

## Validation

- `pytest tests/test_upload_retry.py tests/test_queue_order.py -q`: 61 passed.
- Load check `scripts/replay_flaky.py --seed 7`: order held across 200 injected faults.
- Limit: worst-case delivery delay grows under sustained faults; follow-up in #418.
- Spec: #402.
```

## Pairs with

- `gate-before-push`: run the repository's own gates locally first so the described validation is already green.
- `github-pr-shepherding`: classify the PR from GitHub state and drive it to merged or explicitly blocked.
- `stacked-diff-discipline`: keep each stacked member small and drafted so each description stays truthful.
- `evidence-claim-boundaries`: decide what a given piece of evidence actually supports before you claim it in the validation section.

## DONE means

A reviewer who never saw the agent conversation can state the behavior change, the rationale, and the validation from the title and body alone; the title satisfies the repository's title check; and the validation section names the checks run, their outcome, and material limits — without pasted operational output or claims beyond what was verified.

Sources: repository title/commit gating practice; conventional-commit release gating; shift-left test evidence.
