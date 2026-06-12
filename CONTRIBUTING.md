# Contributing

This repository is a stable public skill catalog. Keep additions few, sharp,
and reusable without local context.

## Skill Naming

- Use lowercase descriptive kebab-case.
- Name the practice or workflow, not the author, organization, model, or tool.
- Do not add vendor prefixes, brand names, `claude-`, `ai-`, or similar noise.
- Prefer a noun phrase for a discipline, such as `process-aware-done`, or a
  verb-first workflow name when the skill is an ordered procedure.
- Keep names stable once published. Removing or renaming a skill is a breaking
  public API change.
- Check upstream catalogs before adding a name. If an upstream skill already
  uses the same name, only reuse it when this repository intentionally provides
  the higher-precedence version, and document that decision in the same change.

## Graduation Bar

A skill belongs here only when it changes agent behavior on real work and a
stranger could use it without our operating context. Incubate rough workflows
elsewhere until the name, frontmatter, structure, and behavior are settled.

Do not publish slogans. Publish compact workflows, checklists, or verification
rules that materially change the result.
