---
name: connector-readiness
description: Use when installing, repairing, or verifying an MCP or other authenticated connector, especially when configuration, authentication, quota, and real artifact access can be mistaken for one another.
---

# Connector Readiness

Treat connector readiness as three separate gates. Passing an earlier gate does
not imply that a later gate works.

## Readiness Gates

1. **Configured**
   - Confirm the plugin or server is enabled in the client that will do the work.
   - Confirm the endpoint, transport, and client version.
   - Restart or start a fresh client process after changing plugin or MCP
     configuration when the client loads tools only at startup.
2. **Authenticated**
   - Run the connector's identity or health command as the same operating-system
     user and client that owns the work.
   - Record the account and plan or seat without exposing tokens.
3. **Artifact-capable**
   - Run the smallest representative read-only operation against the exact
     artifact, object, node, or repository needed by the task.
   - Require a real payload such as a name, dimensions, revision, or metadata.
     A connected status, successful OAuth callback, or tool listing is
     insufficient.

## Error Classification

- **Authentication or permission:** re-authenticate, confirm the account, and
  verify that account can access the target artifact.
- **Invalid argument:** re-extract exact identifiers from the canonical link
  and try a documented orientation call. If unrelated exact artifacts fail
  too, investigate the client wrapper or server schema instead of repeatedly
  guessing identifiers.
- **Quota or plan limit:** stop retrying. Preserve the exact server response,
  check current official plan guidance, and distinguish a server-side quota
  from broken local configuration.
- **Client or model incompatibility:** update the client, start a fresh
  process, and rerun the artifact-capability gate.
- **Timeout or oversized response:** narrow the operation to a smaller object
  without substituting an unauthoritative source.

## Design-Source Connectors

For design-to-code work:

- Use the provider's design-to-code skill before its primary context tool.
- Copy an exact frame or layer link; do not guess a node identifier.
- Prefer the primary design-context call. Use identity, metadata, or screenshot
  calls only for diagnosis and orientation.
- If a live call is quota-blocked, label any accepted repository snapshot or
  export as a fallback with immutable lineage. Do not describe fallback data as
  a successful live connector read.

## Remote Worker Verification

Configuration may differ by user, client, host, or already-running session.

- Inspect readiness under the worker's real user and login environment.
- Verify both the client's connector list and a representative tool call.
- Inspect the active worker transcript or trace for the exact call and result.
  A healthy connector in a different client does not prove the worker used it.
- Record the client, version, user, session, target identifier, operation, and
  observed outcome.

## Done

Call the connector ready only when the intended worker returns a real payload
from the required artifact. If quota, permissions, or external state prevents
that, report the connector as configured or authenticated but not
artifact-capable.
