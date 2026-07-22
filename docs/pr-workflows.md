# PR-first workflows and mergetrain

mergetrain does not make pull requests obsolete. They solve different primary
problems:

- A pull request is a proposal for people and policy systems to review.
- A mergetrain job is a committed unit of agent work to assemble, gate, and
  integrate.

The distinction matters when several coding agents finish small branches in the
same repo. Treating every branch as a PR preserves review and governance, but it
also turns the operator into a PR author, CI watcher, merge-order coordinator,
and conflict resolver. Treating those trusted branches as one train removes
that ceremony, but deliberately gives up the forge's review surface.

## Trade-offs at a glance

| Dimension | PR-first workflow | mergetrain |
|---|---|---|
| Unit of coordination | One proposed change for human review | One committed agent branch; several jobs can form one train |
| Human review | Inline comments, approvals, ownership rules, and discussion are first-class | No review UI; review must happen before enqueue or through another workflow |
| Combined correctness | Plain per-PR CI can miss interactions; a native merge queue can test merge groups | The exact assembled train is gated before its atomic push |
| Failure isolation | Usually one failing PR/check at a time; cross-PR failures depend on the forge queue | Small trains isolate linearly; larger trains bisect to an individual failure or semantic conflict |
| Latency and CI use | Every branch normally pays PR creation, remote scheduling, and CI latency | Local gates can run once for a successful batch; failed batches require additional isolation runs |
| Infrastructure | Hosted forge, branch rules, webhooks, and CI service | A local runner, SQLite state, Git worktrees, and configured shell commands |
| Privacy and portability | Metadata and CI execution live in the selected platform | Queue state and gates stay local; only configured Git and verify traffic leave the machine |
| Distributed collaboration | Strong: reviewers can work asynchronously from anywhere | Weak: one machine owns the queue and runner at a time |
| Recovery | The forge owns queue availability and server-side state | The operator owns the machine; durable markers reconcile ambiguous pushes against the remote |
| Governance | Strong fit for required approvals, protected branches, compliance, and external contributors | Direct pushes must be allowed by repository policy; a rejected protected ref is parked `blocked` |

## Where mergetrain is stronger

- **Many small agent branches.** Agents commit, enqueue, and stop instead of
  creating and babysitting a PR for each execution task.
- **The final combination is the product.** Gates run against the exact ordered
  train, not merely each branch in isolation. A joint failure is identified
  before anything is pushed.
- **Local feedback matters.** Existing test, lint, build, and security commands
  run without waiting for a hosted CI scheduler or installing a forge app.
- **The integration decision is machine-readable.** Exact train identity,
  lease-fenced ownership, explicit deploy intent, and JSON `next_action` values
  keep agents from guessing.
- **A laptop failure is an expected state.** Write-ahead markers and pending refs
  let recovery ask the remote what landed instead of blindly pushing again.
- **The Git provider is not the workflow.** The same runner can target any Git
  remote and can present the operation as deploy, integrate, or push.

## Costs and limitations

- **It is not code review.** There are no inline comments, reviewer assignment,
  approval rules, or web discussion attached to a job.
- **One operator owns reliability.** The runner machine, credentials, disk,
  process supervision, config trust boundary, and gate environment are your
  responsibility.
- **Direct integration may violate repository policy.** Protected branches or
  required-PR rules correctly reject the push; mergetrain does not bypass them.
- **Local state is not a shared organizational audit system.** Git commits and
  remote refs remain durable, but queue events and raw logs need separate
  retention if an organization requires centralized evidence.
- **Batching has a failure cost.** A green train saves repeated gate runs; a red
  train spends extra runs isolating the bad job or interaction.
- **The project is pre-1.0.** Its operational model is implemented and tested,
  but teams needing a mature hosted control plane should prefer their forge's
  native workflow.

Review the [security boundary](security.md) before enabling unattended jobs and
the [failure modes](failure-modes.md) before allowing direct integration.

## Where PR-first is stronger

Use individual PRs when the branch itself is a conversation or approval unit:

- another person must review the diff before it can integrate;
- CODEOWNERS, required approvals, compliance, or branch protection are policy;
- contributors do not share trust or local machine access;
- remote CI is the authoritative build environment;
- the team collaborates asynchronously across machines and time zones; or
- the change needs a durable design discussion more than low landing latency.

A forge-native merge queue is the natural extension in that environment. It
keeps PR review and policy while serializing accepted changes and, depending on
the forge, testing a merge group. mergetrain's advantage is not that a hosted
queue is incapable; it is that a local agent operator may not need the hosted
PR lifecycle for every trusted branch.

## Hybrid patterns

### 1. Direct integration for trusted agent work

Use mergetrain end to end when one operator owns the repo, the configured refs
allow direct pushes, and the branches have already been reviewed through the
agent session or local diff. This is the lowest-ceremony path.

### 2. One review branch, one PR

Point mergetrain at a branch where direct pushes are allowed, validate and push
the assembled train there, then open one PR from that review branch to the
protected target. This preserves one human approval boundary without creating a
PR for every agent execution branch. The operator still owns review-branch
synchronization and must ensure the PR head is the exact validated train.

### 3. Split by change class

Use mergetrain for small, trusted maintenance work and individual PRs for API,
security, data-model, or product changes that deserve discussion. The boundary
should be repository policy, not an agent's judgment.

### 4. PR-first with local preflight

Run mergetrain in validation-only mode to test an expected combination locally,
then keep the normal PR and forge-queue process as the authority. This catches
interactions early, but the later PR merge group must still run its own checks;
local validation is not proof that the forge assembled the same commit.

## Decision rule

Prefer mergetrain when **several trusted local agent branches are execution
units and integration throughput is the bottleneck**. Prefer PR-first when
**each branch is a review, governance, or distributed-collaboration unit**.
Use both when agent throughput is valuable but one final human approval boundary
must remain.
