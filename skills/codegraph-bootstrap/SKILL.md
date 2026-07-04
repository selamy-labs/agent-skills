---
name: codegraph-bootstrap
description: Use when starting code exploration or implementation in a repository where CodeGraph may be available. Ensure CodeGraph is installed/initialized when authorized, then prefer CodeGraph queries for semantic code context.
---

# CodeGraph Bootstrap

Use CodeGraph as the first semantic code-navigation path when it is available.
The goal is to give the agent indexed symbol, call-path, and impact context
before falling back to file-by-file grep/read exploration.

## Workflow

1. Check for an initialized project index:

   ```sh
   codegraph status .
   ```

2. If `codegraph` exists but the project is not initialized, run:

   ```sh
   codegraph init .
   ```

   Do this only inside the target repository. Do not initialize a home
   directory, filesystem root, vendored dependency cache, or generated build
   directory unless the user explicitly asks.

3. If `codegraph` is missing, look for the environment's declared installer or
   package source before using an ad hoc install command. Check repo docs,
   bootstrap scripts, dotfiles, Nix flakes, package manifests, container images,
   or other source-owned provisioning files that already mention CodeGraph.

   ```sh
   rg -n "codegraph|CODEGRAPH_" . "$HOME/.config" 2>/dev/null
   ```

4. Install CodeGraph only when authorized:

   - Authorized: the user asked for installation, the task is explicitly a
     bootstrap/provisioning task, or a source-owned installer exists for the
     environment.
   - Not authorized: a production/shared host with no declared installer, a
     read-only review task, an environment where dependency installation is
     disallowed, or any case where the user asked not to change the host.

5. When authorized, prefer the environment's declared installer. If no declared
   installer exists but the user explicitly authorizes a direct user-local
   install, use the upstream installer:

   ```sh
   curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh
   ```

6. After installation or initialization, prefer:

   ```sh
   codegraph explore "question or symbol area" --path .
   codegraph node SymbolName --path .
   codegraph impact SymbolName --path .
   ```

   If the MCP tool is available, use `codegraph_explore` instead of the CLI for
   the same semantic lookup.

## Guardrails

- Keep installation declarative where the environment has a dotfiles, Nix,
  package-manager, image-build, or IaC source of truth. Add or update that
  source instead of leaving a live-only mutation.
- Do not run `codegraph install --yes` on a host whose agent config is
  source-controlled unless that mutation is also codified or the host-local file
  is intentionally outside shared config.
- Do not block the task if CodeGraph cannot be installed or initialized. State
  the reason, then continue with normal repository tools.
- Treat `.codegraph/` as local generated index state. Do not commit it unless
  the repository explicitly tracks it.
