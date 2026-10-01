---
name: radical-brevity
description: Use as a final edit for prose on any communication surface, including email, chat, documents, commit and PR descriptions, review comments, and prose in diffs. Cut words while preserving meaning, context, accuracy, tone, and action.
metadata:
  audience: universal
---

# Radical Brevity

Give the reader the information they need in the fewest words that still serve them. Brevity is a revision method, not a word limit. Spend your editing time so the reader spends less time decoding the message.

## The two-stage method

1. **Write the full message.** Identify the reader, what they already know, and what they need to understand or do. Follow any format, accessibility, legal, or disclosure requirements of the surface. Ground factual claims before shortening them.
2. **Mark the load-bearing information.** Preserve the point, relevant context, concrete facts, scope, uncertainty, material caveats, and any requested action or deadline. Preserve evidence needed to verify a claim and courtesy that serves this reader.
3. **Cut in descending order of waste.** Remove repeated ideas and sections; generic openings and closings; commentary about the writing process; praise of the work; inflated verbs; filler transitions; and details the reader can reach through a clear link. Then shorten sentences. Prefer a direct verb and a concrete noun over an abstract phrase.
4. **Rebuild for the reader.** Put the point or request where it can be found quickly. Add back a word or sentence if the cut made a claim ambiguous, hid a tradeoff, changed its strength, sounded abrupt, or forced the reader to ask for context.
5. **Compare the two versions.** Check every load-bearing item against the original. Do not add a promise, certainty, or fact while editing. Stop when another cut would cost meaning, accuracy, warmth, or ease of action. A shorter message that takes longer to understand has failed.

## Apply it across surfaces

- In a commit message, change summary, or PR description, retain the behavior, reason, validation, and material limits. Use the repository's required template and pair with `pull-request-writing` for PR structure. A terse title cannot carry facts that reviewers need in the body.
- In a diff, edit prose-bearing lines such as comments, documentation, and user-facing strings. Remove stale or redundant commentary, while keeping explanations of non-obvious decisions. Do not shorten code, identifiers, interfaces, or diagnostic messages merely to reduce characters.
- In email, text, chat, or spoken updates, keep the relationship and the ask clear. A greeting, thanks, or acknowledgment can be the shortest respectful choice. Include the needed date, owner, and next step; avoid a bare status that makes the recipient chase context.
- In long documents, cut repeated claims before trimming sentences. Keep headings, examples, definitions, and citations when they help a reader find or verify the point.

## Remove generated-prose residue

Watch for interchangeable openings ("I hope this finds you well"), self-commentary ("It's worth noting"), unsupported superlatives, repeated summaries, padded lists, and ceremonial conclusions. Cut them when they add no information or useful human tone. Keep a phrase that genuinely fits the audience; do not turn this into a banned-words list.

## Example

**Before:** "I wanted to reach out and provide a quick update regarding the migration. We have successfully completed the database migration, and I think it is important to note that the API deployment is still pending. We are currently planning to deploy it on Thursday. Please let me know if you have any questions."

**After:** "The database migration is complete. The API deployment is planned for Thursday."

The revision keeps the completed work, pending work, and timing. If the recipient must approve the deployment, add that request explicitly.

## Done means

The intended reader can identify the point, its support and limits, and any action without recovering missing context from the author. Every remaining sentence earns its place; every removed sentence leaves the message intact.
