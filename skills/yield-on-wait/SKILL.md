---
name: yield-on-wait
description: Use when CI, deploys, image builds, infrastructure applies, certificates, queues, or other long-running waits would otherwise stall an agent. Requires a durable checkpoint before switching work.
---

# Yield On Wait

Do not spend agent time watching a spinner. If the only remaining action is a
long wait, checkpoint the wait and move to other useful work.

A yield parks the current stage; it never completes the parent task. Retain the
existing continuation and owner so the parent delivery and cleanup obligations
stay tracked. See `process-aware-done` for the done standard and
`ephemeral-workspace-lifecycle` for the cleanup disposition.

## Trigger

Use this skill when a wait is likely to exceed about two minutes:

- CI checks or release workflows
- image builds or package publishes
- infrastructure plan, apply, sync, or provisioning
- certificate, DNS, or load-balancer readiness
- runner pickup or job queue latency
- delegated work in another lane
- any process where the next action depends on an external status change

## Checkpoint

Before switching away, write a durable checkpoint with:

- awaited artifact: PR, run, job, deploy, ticket, process ID, or URL
- where to check it
- current evidence and last known status
- resume condition for success
- failure condition and first diagnostic step
- exact next action after success or failure
- the existing item or continuation and owner responsible for the remainder
- owner and deadline if the wait can stall indefinitely

The checkpoint is the working memory. It must be specific enough that another
agent can resume without the original conversation.

## Wait Identity

Prefer the existing native wait or push mechanism. Bind observation to the
producer's task, run, or job handle. When only process observation is available,
record the producer's PID and start time, including its wrapper/child relation
when relevant. A substring search can match the observer itself or miss a
wrapped producer; distinguish the watcher from the work it watches.

An observation timeout or watcher exit does not prove producer termination and
does not authorize a restart. Verify the producer's terminal result or actual
termination, then check the resulting artifact before advancing dependent work.
If the result remains unknown, preserve the existing run and checkpoint the
uncertainty instead of launching a duplicate or adding a polling service.

## Yield

- For short waits, use a bounded in-turn poll.
- For longer waits, mark the current stage waiting using the existing queue or
  continuation convention, with completed evidence and the checkpoint.
- Reuse the existing continuation and owner. Name the tail link or ID in the
  current item before switching away when an existing tail owns the wait;
  otherwise retain the checkpoint and owner in the current item. Create a
  separate continuation only when needed by that queue's convention, not a new
  issue or controller for every wait. Keep unmet parent work open.
- Switch to the next queued or unblocked work item.
- Prefer work that is independent of the parked wait.
- At natural boundaries, check parked waits through their existing handles.
- If a parked wait becomes the only remaining work, yield through its existing
  continuation or native wait; retain the parent until its criteria are met.

## Checkpoint As Queue Item

When a queue exists, the durable queue item is usually a better carrier than
the current turn's memory. Use this shape:

- update the existing item with evidence completed so far and waiting status
- retain its owner and priority, or use its existing continuation
- include the exact artifact link or ID, check command, success condition,
  failure condition, and next action
- let the assigned worker resume from the checkpoint without the original thread

This prevents a wait from becoming "work in progress" that survives only in a
single process or conversation.

## Resume

When the wait completes:

- read the checkpoint first
- verify the real artifact, not only the status label
- continue the recorded success or failure path
- update the checkpoint or close it with trajectory evidence

## Anti-Patterns

- watching CI or deploy logs for long stretches without doing other work
- switching away without a checkpoint
- checkpointing only "waiting on CI" with no run link, resume condition, or
  next action
- leaving parked waits without a stall alarm or bounded follow-up
- parking a wait and calling the parent delivery done with unmet criteria
- creating duplicate continuations, workers, or controllers for the same wait
- treating watcher exit or an observation timeout as the producer's completion
- treating green CI as a product or deploy verification when the user asked for
  a real artifact

## Done Standard

A checkpoint makes a wait resumable; it does not complete the awaited work.
Complete that work only after its terminal result and real artifact have been
verified. The parent delivery stays open until its own artifact and cleanup
receipts exist and its acceptance criteria are met.
