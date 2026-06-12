---
name: yield-on-wait
description: Use when CI, deploys, image builds, infrastructure applies, certificates, queues, or other long-running waits would otherwise stall an agent. Requires a durable checkpoint before switching work.
---

# Yield On Wait

Do not spend agent time watching a spinner. If the only remaining action is a
long wait, checkpoint the wait and move to other useful work.

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
- owner and deadline if the wait can stall indefinitely

The checkpoint is the working memory. It must be specific enough that another
agent can resume without the original conversation.

## Yield

- For short waits, use a bounded in-turn poll.
- For longer waits, prefer closing or pausing the current item with evidence so
  far and creating a scoped verification tail. The tail body should be the
  checkpoint: awaited artifact, check location, resume condition, and
  success/failure actions.
- Switch to the next queued or unblocked work item.
- Prefer work that is independent of the parked wait.
- At natural boundaries, poll parked waits before pulling more work.
- If a parked wait becomes the only remaining work, requeue a scoped
  verification tail instead of holding the original directive open.

## Checkpoint As Queue Item

When a queue exists, the durable queue item is usually a better carrier than
the current turn's memory. Use this shape:

- close or mark the broad item with evidence completed so far
- create a narrow verification tail at the right priority
- include the exact artifact link or ID, check command, success condition,
  failure condition, and next action
- let the next worker resume from the tail without needing the original thread

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
- holding a broad directive open when the only remaining step is a long wait
  that could be represented as a scoped verification tail
- treating green CI as a product or deploy verification when the user asked for
  a real artifact

## Done Standard

A yielded wait is complete only when the real awaited artifact has been checked
or a scoped blocker tail exists with enough evidence for someone else to
resume.
