---
name: plugin-authoring
description: Use when packaging skills, commands, agents, or hooks into a Claude Code plugin and publishing to a marketplace. Get the directory structure, plugin.json and marketplace.json manifests, version bump, and local-test loop right so the plugin installs cleanly and is version-pinnable.
---

# Plugin Authoring

A Claude Code plugin bundles skills (and optionally commands, agents, hooks, MCP
servers) into one versioned, installable unit distributed through a marketplace.
Getting the structure and manifests right is what makes it install cleanly and
pin to a known version — the failure modes are all in the layout and the JSON.

## Structure

```
my-plugin/
  .claude-plugin/
    plugin.json          # the plugin manifest
  skills/<name>/SKILL.md
  commands/<name>.md     # optional
  agents/<name>.md       # optional
  hooks/                 # optional
```

The marketplace is a separate repo (or root) with a `marketplace.json` listing
the plugins it offers.

## Manifests

`plugin.json` — identity + version (the thing consumers pin):

```json
{
  "name": "my-plugin",
  "version": "0.2.0",
  "description": "One line on what it provides",
  "author": { "name": "...", "email": "..." }
}
```

`marketplace.json` — the catalog entry pointing at the plugin:

```json
{ "name": "my-marketplace",
  "plugins": [{ "name": "my-plugin", "source": "./my-plugin" }] }
```

## The loop that prevents broken releases

1. **Build** the structure; keep one capability per skill.
2. **Validate locally** — install the plugin from a local path and confirm the
   skills/commands actually load before publishing. A manifest that parses but
   ships a skill that won't load is the classic invisible break.
3. **Version deliberately** — bump `plugin.json` `version` on every change;
   consumers pin to it, so an unversioned change silently shifts everyone.
4. **Publish** by updating the marketplace entry, then have a consumer install
   the pinned version and verify it resolves.

## Discipline

- **Version-pin in consumers**, never float — reproducibility beats "latest."
- **One bundled unit, declared per workload** — each workload's config names the
  plugin + version it gets, so what's installed is auditable.
- **Don't ship exec scripts inside skills** — keep skills as instructions;
  put any tooling in the plugin's scripts/commands, audited separately.

---

_Adapted from the MIT-licensed [softaworks/agent-toolkit](https://github.com/softaworks/agent-toolkit) plugin-packaging skill (guidance only; upstream's generator script intentionally omitted)._
