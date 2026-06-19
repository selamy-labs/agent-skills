---
name: tooling-first-automation
description: Use when a task can be performed through existing callable tooling, MCP servers, browser automation, CLI wrappers, or service APIs before writing bespoke scripts or ad hoc automation. Especially relevant for browser/UI automation, external-system actions, and repeated operational workflows.
---

# Tooling-First Automation

Before writing new automation, inspect the tools already available in the
environment and prefer the narrowest existing callable path that fits.

## Workflow

1. Check the configured tool surface: MCP servers, built-in browser tools,
   repository scripts, CLIs, SDK helpers, and service-specific wrappers.
2. Use a first-party or official integration when it covers the task. For
   browser work, prefer a configured browser automation MCP or built-in browser
   tool before hand-writing Playwright, Puppeteer, Selenium, or CDP scripts.
3. If no existing tool fits, write the smallest scoped script needed and explain
   why the existing tools were insufficient.
4. If the right tool exists but is missing dependencies, broken, or unavailable
   in the current environment, fix or codify the tool installation/configuration
   in the owning source of truth before treating a one-off script as normal.
5. Verify the real tool path works with a representative action, not just that
   a package or config entry exists.

## When Not To Use

- The user explicitly asks for a from-scratch implementation.
- The existing tool cannot perform the required action safely or with enough
  observability.
- A short local script is the repository's established interface for the task.

## Anti-Patterns

- Writing bespoke browser automation while a browser MCP or built-in browser
  tool is configured and working.
- Copying an API call into prose or shell snippets when a typed MCP/server tool
  already exposes the operation.
- Declaring a tool "available" without launching it or completing a real action.
- Leaving a dependency fix as an operator memory instead of codifying it in the
  source that provisions the environment.
