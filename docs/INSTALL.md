# Installing the `selamy-skills` plugin

This repository is both a **Claude Code plugin** (`.claude-plugin/plugin.json`)
and a single-plugin **marketplace** (`.claude-plugin/marketplace.json`). One
versioned unit bundles every skill under `skills/` plus four MCP servers
(`laneq`, `reddit`, `dispatch`, `memory`).

The mechanics below are grounded in the official Claude Code documentation:

- Plugins reference: <https://code.claude.com/docs/en/plugins-reference>
- Plugin marketplaces: <https://code.claude.com/docs/en/plugin-marketplaces>

## What the plugin bundles

| Component | Where it comes from | How it is declared |
| --- | --- | --- |
| Skills | `skills/<name>/SKILL.md` (auto-discovered) | `"skills": "./skills/"` in `plugin.json` |
| MCP servers | published packages launched via `uvx` | `"mcpServers": "./mcp-servers.json"` in `plugin.json` |

Plugin MCP servers start automatically when the plugin is enabled and appear as
standard MCP tools. They go through the same per-server approval as a project
`.mcp.json` (you approve each server once on first use).

## Reproducible (pinned) per-workload declaration

There are **two independent pins** (per the docs):

1. **Marketplace source** — where the `marketplace.json` catalog is fetched
   from. Set in `extraKnownMarketplaces`. Supports `ref` (branch/tag) but
   **not** `sha`.
2. **Plugin source** — where the plugin itself is fetched. This plugin uses the
   relative `"source": "./"`, so the plugin is fetched from the same commit the
   marketplace `ref` resolves to. Pinning the marketplace `ref` to a release tag
   therefore pins both.

Add this to a workload's `.claude/settings.json`. Pin `ref` to the release tag
you have verified (replace `vX.Y.Z`):

```json
{
  "extraKnownMarketplaces": {
    "selamy-labs": {
      "source": {
        "source": "github",
        "repo": "selamy-labs/agent-skills",
        "ref": "vX.Y.Z"
      }
    }
  },
  "enabledPlugins": {
    "selamy-skills@selamy-labs": true
  }
}
```

When the workspace is trusted, Claude Code prompts to install the marketplace and
enables the plugin. To roll the fleet forward, bump `ref` to the next tag in each
workload's `settings.json` — nothing else changes.

> Pin to a **tag**, never a moving branch like `main`. A tag is a reproducible
> install; `main` silently changes under you.

## The four bundled MCP servers

Declared in `mcp-servers.json`. Each launches its published console-script entry
point through `uvx`, pinned to a release **tag** so installs are reproducible:

| Server name | Source repo | Entry point | Pin |
| --- | --- | --- | --- |
| `laneq` | `selamy-labs/laneq` | `laneq-mcp` | tag `v0.4.0` |
| `reddit` | `selamy-labs/reddit-mcp` | `reddit-mcp` | tag `v0.1.0` |
| `dispatch` | `selamy-labs/dispatch-mcp` | `dispatch-mcp` | tag `v0.1.0` |
| `memory` | `selamy-labs/memory-mcp` | `memory-mcp` | tag `v0.1.0` |

In every repo the `mcp` SDK is an **optional** dependency (extra `[mcp]`), and
the MCP entry point only exists from the tagged version above (for `laneq` the
`laneq-mcp` entry point and the `[mcp]` extra are on `v0.4.0`, not the older
`v0.3.0`). Each launch therefore installs the extra and pins to the tag:

```
uvx --from "laneq[mcp] @ git+https://github.com/selamy-labs/laneq@v0.4.0" laneq-mcp
```

Without the `[mcp]` extra the entry point ImportErrors at launch
(`requires the 'mcp' package`); with it, the server starts. To roll a server
forward, cut a new tag in its repo and bump the `@vX.Y.Z` in `mcp-servers.json`.

### Runtime requirements on each workload

- **`uvx`** (from [uv](https://docs.astral.sh/uv/)) must be on `PATH`.
- **git access to the source repos.** `uvx --from "<pkg>[mcp] @ git+https://github.com/..."`
  performs a git fetch at launch, so the host needs read access to each repo. If
  any repo is not publicly readable, configure a git credential helper (or a
  token-bearing remote) before enabling the plugin; otherwise the MCP server
  fails to start. Check the `/plugin` Errors tab if a server does not appear.
- `laneq` resolves its queue database from the `LANEQ_DB` environment variable
  (shared with the `laneq` CLI). Set it on the workload if you need a specific
  queue location.

> Bare PyPI names are intentionally **not** used: the names `laneq`,
> `reddit-mcp`, and `memory-mcp` are already taken on PyPI by unrelated
> projects. Installing by PyPI name would pull the wrong package, so every
> server is sourced from its `selamy-labs` git repo.
