---
name: public-repo-ci-isolation
description: Use when configuring CI for a public or fork-accepting repository, or choosing runners for untrusted pull requests. Untrusted fork code must never run on a trusted self-hosted runner with access to secrets or internal network.
---

# Public Repo CI Isolation

Never run a public repository's CI on a trusted, persistent self-hosted runner.

A pull request from a fork can change the workflow or the code it runs. On a
self-hosted runner that PR executes attacker-controlled code on your own
infrastructure, with whatever that runner can reach: secrets, cloud credentials,
the internal network, the host filesystem, and the runner's registration token.
This is the classic self-hosted-runner-on-public-repo vulnerability.

There is no cost argument against isolating: public repositories get free
hosted CI minutes, so trusted self-hosted runners buy you nothing here.

## When to use

- A repository is public, or accepts pull requests from forks.
- You are choosing `runs-on` for a workflow that runs on `pull_request`.
- You have self-hosted runners and someone proposes pointing a public repo at them.
- A workflow uses `pull_request_target` or otherwise runs with secrets present.

## The rule

- Untrusted-fork CI runs on **ephemeral, isolated, single-use** runners (or
  hosted runners), never on persistent self-hosted ones.
- A self-hosted setup that cannot isolate untrusted PRs must **loud-reject or
  require approval** for them. It must never silently queue them forever or run
  them quietly.
- Trusted self-hosted runners are for trusted work only: the default branch,
  pushes by maintainers, or PRs that a maintainer has explicitly approved to run.

## Safe patterns

- **Hosted by default for public repos.** Point `pull_request` workflows at
  hosted runners. Free minutes, fresh VM per job, no access to your network.
- **Ephemeral single-use runners.** If self-hosted is required, each job gets a
  throwaway runner that is destroyed after one job and has no standing secrets or
  network reach. A leaked registration token then buys one disposable VM, not a
  foothold.
- **Approval gate.** Require a maintainer to approve workflow runs for first-time
  or fork contributors before any runner picks the job up. Make the unapproved
  state visible, not a silent hang.
- **Split trusted from untrusted.** Run untrusted build/test on isolated runners;
  run privileged steps (deploy, publish, anything with secrets) only on the
  default branch after merge.

## `pull_request_target` and secret exposure

`pull_request_target` runs in the context of the base repository, so secrets are
available. If you also check out the PR head and execute it, you have handed
attacker code your secrets.

- Do not check out and run fork-PR code under `pull_request_target`.
- Use `pull_request_target` only for trusted, read-only metadata work (labeling,
  triage) that does not execute the PR's contents.
- Never expose secrets to a job that runs untrusted code.

## Anti-patterns

- Pointing a public repo's `pull_request` workflow at a persistent self-hosted
  runner that holds or can reach secrets.
- Letting untrusted fork PRs silently queue forever instead of loud-rejecting or
  gating them on approval.
- `pull_request_target` plus a checkout of the PR head plus a build/test/run step.
- Reusing one long-lived self-hosted runner across trusted and untrusted jobs.

## Done standard

For every public or fork-accepting repo: untrusted-PR jobs land on hosted or
ephemeral isolated runners, no trusted self-hosted runner can pick them up,
unapproved untrusted PRs are visibly rejected or gated rather than silently run,
and no workflow runs fork code with secrets in scope. Make these runner and
approval settings declarative, not a one-off live edit (see [[iac-not-ad-hoc]]).
