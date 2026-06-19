---
name: self-hosted-runner-substrate
description: Use when designing or repairing Kubernetes-backed self-hosted CI runners, especially Actions Runner Controller, runner scale sets, registry pulls, autoscaling, failover, or reproducible infrastructure.
---

# Self-Hosted Runner Substrate

Use this when CI capacity depends on self-hosted runners instead of hosted
runners. Treat runner capacity as production infrastructure, not as ad hoc
pods.

## Workflow

1. Start from the job label and registration boundary:
   - In Actions Runner Controller, the runner scale set name is the label a job
     targets.
   - Active-active failover is hard when two installations need the same label;
     prefer one active pool per label or a deliberate active-passive cutover.
2. Keep the substrate reproducible:
   - Declare the cluster, node pools, controller, runner scale sets, namespaces,
     and pull secrets in infrastructure as code.
   - If anything was created during an incident, import it into state before
     calling recovery complete.
3. Validate runner registration from both sides:
   - Cluster view: controller/listener/runner pods ready and not crashlooping.
   - CI view: a real workflow job starts on the intended label.
4. Model image pulls explicitly:
   - Prefer a known runner image and explicit registry pull credentials.
   - Pre-pull large images when cold node startup is part of the latency budget.
5. Autoscale the node pool with the provider-native path:
   - Do not assume Karpenter exists outside AWS.
   - For managed Kubernetes providers, verify the autoscaler image includes that
     provider's cloud integration; upstream images may not.
6. Kubeconfig and provider auth are part of the design:
   - If the kubeconfig uses an exec credential plugin, CI and operators must have
     that binary on `PATH`.
   - Prefer workload identity or instance principal mechanisms over static keys
     when the cluster tier supports them.

## Done

- A clean infrastructure plan can recreate the runner substrate.
- A real job runs on each advertised label.
- Failure modes are documented: unavailable node capacity, registry pull
  failure, controller/listener crash, auth plugin missing, and label mismatch.
