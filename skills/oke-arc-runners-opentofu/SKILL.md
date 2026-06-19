---
name: oke-arc-runners-opentofu
description: Use when designing, reviewing, or implementing self-hosted GitHub Actions runners on Oracle Kubernetes Engine with Actions Runner Controller and OpenTofu. Covers runner scale sets, GitHub App secrets, OKE kubeconfig, autoscaling, and declarative rollout boundaries.
---

# OKE ARC Runners With OpenTofu

Use this skill for self-hosted GitHub Actions runners on Oracle Kubernetes
Engine managed through OpenTofu and GitOps.

## Source Of Truth

- Declare OKE, node pools, IAM, registry access, and Kubernetes bootstrap in
  OpenTofu modules and GitOps manifests.
- Prefer the official OKE module where its provider constraints fit the root.
  If it requires newer provider major versions than the GitOps/bootstrap root,
  split the stack into a cluster root and a GitOps root instead of forcing one
  mixed provider graph.
- Keep runner scale set values in source. Do not patch live controller objects
  to change images, labels, secrets, or scaling.

## Runner Scale Sets

- Use Actions Runner Controller runner scale sets and listeners, not
  hand-rolled long-lived runner pods.
- Treat each `runnerScaleSetName` as unique to one GitHub App installation and
  one target scope. Active-active failover with the same scale-set name can
  steal or strand jobs.
- Put the runner image behind a pinned tag or digest. The image contract must
  state whether Docker, rootless BuildKit, or a daemonless builder is present.
- Use a pull secret for private registry access and keep it synchronized from
  the secret manager.

## GitHub App Secret

Use a GitHub App credential for ARC's `githubConfigSecret`:

- `app_id`
- `installation_id`
- `private_key`

Store these in the established secret manager and sync them into Kubernetes.
Never print or commit the private key. Preserve PEM bytes exactly when moving
the key between systems.

## OKE Details

- OKE kubeconfig commonly uses an `oci` exec-credential plugin. Every CI,
  deploy, and verification context that runs `kubectl`, `helm`, or GitOps
  health checks must have that plugin on `PATH` with the right cloud auth.
- OKE Basic clusters may not expose the public OIDC issuer needed by keyless
  workload identity flows. Verify the issuer before designing a federation
  flow that depends on it.
- Karpenter is AWS-specific. For OKE burst capacity, use Cluster Autoscaler
  with an OCI-compatible build and instance-principal or workload-identity
  access to resize node pools.
- The stock upstream Cluster Autoscaler image may not include the `oci-oke`
  cloud provider. Pin an OCI-compatible image and verify it starts with
  `cloudProvider=oci-oke`.

## Verification

Before calling the runner substrate ready, verify:

- OpenTofu validates and plans the declared resources.
- GitOps syncs the controller, listener, and runner scale set.
- A real workflow job lands on the expected scale set.
- The runner pod uses the intended image digest and can access required build
  tools.
- Autoscaler can observe pending runner pods and resize the intended node pool.
