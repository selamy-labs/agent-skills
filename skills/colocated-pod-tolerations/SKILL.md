---
name: colocated-pod-tolerations
description: Use when a Kubernetes Job, CronJob, or Pod is pinned to a specific node (via podAffinity, nodeSelector, or nodeAffinity) to share a node or an RWO volume. It must also carry that node's tolerations or it is permanently unschedulable.
---

# Colocated Pod Tolerations

When you pin a Pod to a particular node so it can share something node-local —
most often a `ReadWriteOnce` PersistentVolume already mounted by a StatefulSet
pod, or a GPU/local disk on a tainted node — you express the pinning with
`podAffinity` + `topologyKey: kubernetes.io/hostname`, `nodeSelector`, or
`nodeAffinity`. That makes the scheduler *want* a specific node.

But the target node is frequently **tainted** (dedicated node pools, GPU pools,
spot pools, and StatefulSet-owned nodes commonly are). Affinity does not grant
the right to land on a tainted node — only a matching **toleration** does. A pod
with the affinity but **no toleration** is steered toward a node it is then
forbidden to run on, so it sits `Pending`/`Unschedulable` **forever**.

## Why this is so easy to miss

- It is **silent**. A CronJob just produces `Failed`/never-started runs; nothing
  crashes loudly. The pod template looks correct in review.
- It can lie **dormant**. It works while the node is untainted, then breaks the
  day a taint is added (node pool migration, dedicating a node to a StatefulSet)
  — long after the manifest was written.
- Real incident: halt-recovery CronJobs were dead for ~46 hours because they
  co-located onto a tainted node without inheriting its tolerations. Every
  scheduled run silently failed; nobody was being recovered.

## The rule

**Any pod you pin to a node must carry the full toleration set of that node.**
If you copy `affinity`/`nodeSelector` from another workload to colocate with it,
copy that workload's `tolerations` in the same edit. Affinity and tolerations
are a pair when targeting tainted nodes — never one without the other.

```yaml
spec:
  affinity:
    podAffinity:                       # land on the same node as the owner pod
      requiredDuringSchedulingIgnoredDuringExecution:
        - labelSelector:
            matchLabels: { app: stateful-owner }
          topologyKey: kubernetes.io/hostname
  tolerations:                         # REQUIRED — the owner's node is tainted
    - key: dedicated
      operator: Equal
      value: stateful-owner
      effect: NoSchedule
```

## Prefer not to contend at all

Sharing one node to reach an RWO volume is a smell. Before colocating, ask
whether the job can read a **snapshot/clone** of the volume, or whether the
volume should be `ReadWriteMany`, so the job needs no node pinning and no
tolerations. Colocation is the fallback, not the default.

## Make it a regression you cannot reship

Add a render/template test that fails the build when a manifest declares
node-pinning without tolerations:

1. Render the chart/manifests (`helm template`, `kustomize build`, or your
   generator).
2. For every Pod-spec-bearing object, assert: if it has
   `podAffinity`/`nodeSelector`/`nodeAffinity` targeting a known tainted
   pool, it also has a toleration matching that pool's taint.
3. Fail the test otherwise.

A unit test on the rendered YAML catches the silent class before it ships;
a cluster smoke check (`kubectl get pods --field-selector status.phase=Pending`
after deploy, alert on age) catches the dormant case after a taint appears.
