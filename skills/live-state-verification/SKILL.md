---
name: live-state-verification
description: Use when verifying service health, deployed state, cluster objects, credentials, or automation results where the wrong auth context can produce false empty results.
---

# Live State Verification

Use this when a result depends on the real system state, not just logs or a local
assumption.

## Verification Rules

- Use the correct identity and environment for the artifact being checked.
- Do not suppress stderr on presence or health checks; auth errors and empty
  results can look identical after redirection.
- Corroborate important claims from at least two views.
- Prefer direct artifact checks over proxy logs.

## Procedure

1. Name the artifact:
   - deployed revision, job run, pod, service endpoint, repository setting,
     secret version, document, email, or workflow result.
2. Confirm the auth context:
   - account/user
   - cluster/project/region
   - kubeconfig or exec plugin
   - token scope or installation
3. Run the primary check without hiding errors.
4. Run a second check from a different plane:
   - API plus UI/CLI
   - controller status plus workload behavior
   - repository metadata plus branch protection behavior
   - pod environment plus application-level operation
5. Preserve concise evidence:
   - command class, artifact id, timestamp, observed status
   - do not store credentials or sensitive payloads

## Failure Modes

- Missing exec credential plugin on `PATH`.
- Checking the wrong cluster or project.
- Token has read access to one repo/org but not the target.
- Stderr redirection hides a permission error.
- A controller is healthy while the workload it manages is not.
- A log says "sent" or "applied" but the external artifact never changed.

## Done

- The claim is backed by the real artifact or two independent views.
- Any uncertainty is stated as uncertainty rather than upgraded to "verified."
