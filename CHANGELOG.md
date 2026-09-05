# Changelog

## 3.0.7 - 2026-09-06

- Replace the removed duplicate-enqueue option in error messages with supported
  status and retry guidance. Identify the owning job when branch changes, failed
  gates, or semantic conflicts require a clean repair commit and a retry.
- Align agent instructions and recovery documentation with the existing retry
  workflow, including the distinction between queued, validated, and deployed.
- Include reproducible parallel-development and daemon lifecycle experiments;
  these diagnostic records do not establish a general speed improvement.

## 3.0.6 - 2026-09-05

- Make the benchmark runners use platform temporary directories and make
  their synthetic subprocess test portable across Linux, macOS and Windows.

- Clarify existing-queue handoff even when only one branch is ready. Queue
  counts alone do not establish health, runner ownership, or recovery needs.
- Add the current v3 status, diagnostic and job-evidence command reference to
  generated agent guidance. Identify removed doctor syntax and preserve the
  distinction between explanation, inspection and execution authority.
- Record a frozen 32-run paired operator-guidance comparison and the earlier
  rejected discovery-copy experiment. Public discovery descriptions remain
  unchanged; this release does not claim automatic-activation improvement.

## 3.0.5 - 2026-09-05

- Update the dashboard build's Browserslist dependency to 4.28.9 and refresh
  its compatible browser-data dependencies, resolving the reported cache-growth
  and custom-stats advisories without changing the packaged dashboard bundle.

- Fix dashboard and Hub snapshots remaining stale after SQLite reuses WAL
  space. Share one read-only change observer per cached repository, detect
  concurrent commits and database replacement, and release observers on Hub
  removal or server shutdown without pinning a read transaction.

- Preserve diagnostic truth across human and structured output: distinguish
  semantic conflicts from textual merge conflicts, show blocked reasons in
  `inspect`, and always render repository health plus a next action in
  `status`.

- Make the Python quickstart gate checkout-clean by default, include dirty
  paths when a gate changes the assembled tree, and protect the interactive
  deployment approval path with targeted tests and a critical coverage floor.

- Remove design PNGs from the current source tree, keep historical
  visual evidence linked to the immutable v3.0.4 tag, and exclude design-only
  assets from source distributions.

## 3.0.4 - 2026-09-04

- Complete the Claude Code community-plugin runtime pilot from issue #212:
  describe every MCP tool and server version, keep the two-input MCP enqueue
  contract while safely resolving a branch's unique live Git worktree, remove
  stale global-install guidance, and clarify that agents do not select train
  IDs or supply plan hashes even though structured evidence may retain
  identifiers. Harden worktree discovery against forged porcelain attributes,
  stale registrations, and foreign repositories; document clean gate output
  and the shell security boundary. The owner-operated Claude Code session
  passed discovery, read-only safety, ordered exact-SHA handoff, confirmation
  decline, and successful deploy controls.

- Make the Claude Code plugin self-contained for community-directory review:
  launch the exact released MCP extra through `uvx`, bind its manifest version
  to the package release, and include install, prompt, privacy, security,
  troubleshooting, and support documentation. Release checks now reject
  Claude runtime or version drift.

- Add a problem-first, telemetry-free GitHub Pages landing page and a single
  evidence-backed submission packet for the Claude community marketplace,
  OpenAI Developer Showcase, Show HN, Reddit, and curated lists. Keep external
  distribution copy separate from the stable product grammar. Package the
  static site in the sdist so its included landing-page tests remain runnable.

## 3.0.3 - 2026-09-04

- Add a mechanically graded Codex safe-handoff harness and complete the fixed
  20-run cell at 19/20 with zero direct pushes or unauthorized actions. Clarify
  the generated protocol so every named finished branch is enqueued in order,
  stopping after the last enqueue, and so "queue for validation" cannot be
  interpreted as permission to execute validation.

- Record a controlled Codex catalog-trigger diagnostic. Three narrower
  discovery-copy candidates kept matched suitable discovery at 5/5 but reduced
  the five known negative activations only to 4, 2, and 4, so the public
  discovery metadata remains unchanged.

- Clarify the local trust boundary in the README: mergetrain has no account,
  hosted control plane, OAuth app, or product telemetry. Add a launch-ready Show
  HN first-comment draft and preserve the completed Codex recommendation and
  negative-control executions as owner-reviewed benchmark evidence. Suitable
  discovery is 20/20 and negative primary recommendation is 0/20; the separate
  negative-activation overhead gate remains open for a controlled trigger test.

- Separate discovery selection, false-positive recommendation, unnecessary
  activation, explanation quality, and authority-safety metrics in discovery
  benchmark version 2. Preserve validation of immutable version 1 results and
  hide the private trial manifest while an agent process runs.

- Add a benchmark-only runner and deterministic scorer for the frozen
  product-name-free discovery corpus. Results now preserve prompt and metadata
  revisions, exclude contaminated or operationally invalid trials, enforce
  per-client 20-fixture denominators, and retain zero-tolerance push and
  authority violations without changing product runtime semantics.

- Add a Git-installable Codex marketplace and native plugin that package the
  problem-first skill with the existing five-tool, release-pinned MCP server.
  Keep its descriptions and starter prompts bound to the canonical discovery
  metadata without changing product commands or authority boundaries.

## 3.0.2 - 2026-09-04

- Bound the problem-first MCP Registry description to the registry's
  100-character schema limit while keeping it in the canonical discovery
  metadata source.

- Align PyPI, MCP Registry, Claude, README, and LLM-facing discovery metadata
  around the parallel-agent integration problems mergetrain solves. Add a
  canonical metadata source and product-name-free benchmark fixtures without
  changing queue, deployment, recovery, CLI, or MCP semantics.

- Package the existing five-tool MCP adapter and ordinary-agent protocol as a
  native agy plugin. Its skill preserves the `status → enqueue → stop` flow and
  the existing human-gated deploy and recovery boundary.

- Redact persisted job notes before every structured projection, cap public
  note-derived text at 1,000 characters, and publish explicit
  `reason_truncated`, `note_truncated`, or `message_truncated` metadata. This
  covers compact status, serialized jobs, outcomes, and the legacy no-event
  inspect fallback, so valid CLI JSON remains safe when the MCP adapter returns
  it unchanged.

- Pin every multi-query status, Hub, and dashboard read to one SQLite snapshot
  so aggregate counts, attention rows, and the resulting next action cannot
  describe different commits. Share verification-failure classification across
  the Python projection, Hub, browser badge, current train, and history views.

- Persist an internal deployment-generation ID, destination identity, and
  verification-policy identity before remote I/O. `verify --job` now runs the
  original available policy once and resolves every member of that deployment
  atomically; changed, missing, and legacy-unprovable policies require explicit
  `--ack succeeded/failed` instead of silently succeeding without a hook.

- Keep every known post-push verification failure in Attention until an
  explicit recheck or acknowledgement resolves it. A later unrelated deploy no
  longer hides unresolved health evidence. This safety correction changes the
  meaning introduced by contract 3, so machine output advances to contract 4
  without adding a CLI verb, flag, config field, state group, or MCP tool.

- Base the minimal-gate warning on effective gates, so a legacy configured copy
  of the built-in `diff-check` no longer suppresses the no-project-gates
  warning.

## 3.0.1 - 2026-09-03

- Keep unknown post-push verification and the latest deployed generation's
  known verification failures in Attention. Later deploys supersede an older
  production-health result in current status without rewriting its evidence.
  Derive the next-action code, exact job target, command, approval class, and
  stable reason from one planner so mixed failure types cannot produce a
  mismatched instruction. Existing `verify --job` can now re-run a failed
  verification and clear the Attention state after success.

- Make `status` a no-create observation on repositories without queue state,
  report missing Git repositories, remotes, or integration refs as degraded,
  and refuse to recommend enqueue until the configured base can be resolved.
  Warn when no project gates are configured without changing deployment
  policy.

- Make omitted enqueue worktrees resolve from the explicit repository rather
  than the process working directory, including MCP servers launched elsewhere.
  Root command typos now list only the six permanent public verbs. Contract-3
  fingerprints record the additive warning, target, and reason fields.

## 3.0.0 - 2026-09-03

- Replace the implementation-shaped CLI with six permanent product verbs:
  `init`, `status`, `enqueue`, `validate`, `deploy`, and `inspect`. `status
  --diagnose` absorbs doctor, `validate` and `deploy` absorb runner modes, and
  `reconcile --apply` absorbs safe stranded-claim recovery. Removed v2
  invocations fail with the typed `removed_interface` error and an exact
  migration command; no compatibility aliases remain.

- Make `deploy` the complete human workflow. It validates queued work when
  needed and renders the sole exact Ready train's tasks, destination, refs, and
  gate policy, confirms interactively, then rechecks the private plan identity
  before atomic push and verification. Direct validation pauses while one train
  is Ready, removing routine train selection. `deploy --json` is
  non-pushing, and human-facing paths no longer ask for train IDs, SHAs, plan
  hashes, preview modes, or one-shot reuse switches.

- Project internal queue detail into five status groups—Waiting, Running,
  Ready, Attention, and Done—with one structured next action. Keep specialist
  evidence, daemon, repair, recovery, cleanup, dashboard, Hub, demo, and MCP
  commands callable but out of default help.

- Reduce the default MCP surface from twelve tools to five and fold bounded
  event/log reads into inspect detail. MCP deploy retains client-side human
  elicitation and exact plan revalidation while its fallback shows only the
  ordinary interactive deploy command.

- Generate a minimal config containing only schema version, project name, and
  gates, and reduce generated task-agent instructions to five core rules. Full
  runtime defaults and all exact-SHA, lease, combined-gate, conflict,
  destination/policy binding, atomic-push, and recovery guarantees remain.

- Synchronize `llms.txt`, `llms-full.txt`, Claude instructions, plugin skills,
  quickstart, and convenience scripts with the v3 grammar. The protocol checker
  now fails CI if those high-signal surfaces reintroduce removed commands or if
  default help stops exposing exactly the six verbs in lifecycle order.

- Establish contract 3 as the long-lived interface: the six CLI names, five
  MCP tools, config-schema-2 readability, error envelope, and existing JSON
  meanings are frozen; 3.x evolves additively and has no planned successor
  grammar. Add contract fingerprints and installed-wheel E2E coverage for the
  simplified surface.

## 2.4.2 - 2026-09-02

- Root production publishing in the protected `main` workflow and signer
  policy instead of the release tag being verified. The publisher now accepts
  a signed tag as data, requires its commit in current main, builds the captured
  commit SHA, and refuses non-main dispatches or a mutable GitHub Release before
  any tag-provided code or PyPI credential is used.

- Separate tag verification, package build, artifact attestation, and PyPI
  publication permissions. TestPyPI rehearsals use the same main-rooted signed
  source resolution, and downstream MCP publication checks out the verified
  release commit.

## 2.4.1 - 2026-09-02

- Reject manually supplied base or head SHAs on a normal ready-checked enqueue
  when they differ from the current integration ref or clean task branch. The
  generated agent contract now directs ordinary handoff to omit compatibility
  SHA flags and use the existing verified default capture.

- Make the agent-adoption harness classify a nonzero launcher exit with no
  trace or repository change as an operational harness error rather than an
  agent behavior failure, and make the documented macOS launch commands
  independent of the task worktree's current directory.

- Require release tags to carry a trusted SSH signature before the publishing
  workflow can build or receive PyPI credentials, with a tracked public
  allowed-signers file for local and CI verification.

## 2.4.0 - 2026-09-02

- Bind unattended deploy approval to the exact execution policy as well as the
  Git destination. Enqueue records the effective gates, command timeout,
  validation-reuse policy and authorization, and verify hooks; daemon claim,
  pre-gate, and pre-push checks fail closed if any of them changes. Legacy auto
  approvals and retries without both identities require fresh approval.

- Make MCP Registry publication resilient to fresh-PyPI metadata propagation
  by retrying the exact manifest command with an isolated uv cache and bounded
  workflow timeout.

- Pin every third-party GitHub Action to an immutable commit, enforce pins in
  the release checker, and generate GitHub artifact attestations for release
  distributions.

## 2.3.1 - 2026-09-02

- Bind approval, audit lookup, atomic push, and crash reconciliation to one
  resolved Git push endpoint. Split fetch/push remotes remain supported;
  multiple push URLs and relative filesystem push URLs now fail closed. Fresh
  per-command sentinel URLs prevent later Git URL rewrites from redirecting the
  frozen endpoint, absolute local paths are canonicalized, and legacy recovery
  markers without an endpoint identity remain parked for manual inspection.

- Make the MCP Registry install the optional MCP runtime through `uvx --from
  mergetrain[mcp]`, and smoke-test the packaged stdio server before Registry
  publication.

- Replace the stale mobile deploy wrapper's duplicated safety logic with the
  CLI's canonical preview and `--expected-plan` flow.

- Ship a self-contained sdist test surface, remove the stale README release
  number, and verify extracted source artifacts in release checks.

## 2.3.0 - 2026-09-02

- Bind unattended approval to a credential-free destination identity and MCP
  confirmation to the CLI's exact deploy-plan hash. Destination changes and
  confirmed-plan policy changes now fail closed before claim or push; retry
  keeps auto approval only for the same destination.

- Capture base and task SHAs by default for normal CLI enqueue, while retaining
  the existing flag and explicit direct-insert escape for compatibility.

## 2.2.0 - 2026-09-02

- Classify semantic conflicts consistently at every multi-job batch size.
  Two or three individually green jobs that fail only in combination are now
  blocked together with reciprocal `conflict_with` evidence instead of being
  split into FIFO-dependent validated or deployed jobs.

- Make MCP deploy summaries describe gate-policy evaluation without promising
  unconditional reruns when path scopes or validated-gate reuse apply, and
  include deployed jobs whose post-push verification is still unknown.

- State that stale-validation diagnostics observe the local integration ref
  without fetching; deploy still fetches and reassembles before any push.

- Migrate the optional MCP adapter to SDK v2 and its `MCPServer`, `Resolve`, and
  `Elicit` APIs. Deploy confirmation now works through the modern
  `InputRequiredResult` retry flow, rechecks train eligibility after the human
  response, remains compatible with older negotiated protocols, and is covered
  by real v2 client transcripts for accept, decline, cancel, unchecked,
  unsupported-client, and changed-state paths.

- Clarify that generated agent-contract sidecars require a link from standard
  root instruction files, and document refreshing them through the existing
  `agent-contract` command without overwriting project configuration.

## 2.1.0 - 2026-09-02

- Make MCP deploy confirmation describe only the selected change set in human
  terms: task intent, destinations, gates, validation evidence, stale-base
  reassembly risk, and the uncapped attention set. Keep opaque train IDs
  internal to selection and execution.

- Treat an explicit bounded request to QA, deploy, verify, and finish as
  unattended approval for that unchanged task scope and destination. Keep exact
  train IDs internal, require human-readable one-shot deploy summaries, and
  stop repeated per-train approval prompts without weakening SHA binding.

- Annotate pending validated trains in `status` and `doctor` when the
  integration ref has advanced since validation. The diagnostic explains that
  deploy remains eligible but will reassemble and rerun gates before push.

- Add a provider-neutral local multi-agent integration benchmark that measures
  exact-SHA handoff, remote safety, conflict recovery, and semantic pair
  isolation with a disposable local bare remote.

- Add a fail-closed Antigravity CLI adapter to the repository-local agent
  adoption harness and record its first fixed `current_init` operational cell.
  All three fully instrumented trials ended in provider `high traffic` errors,
  so the evidence reports availability failure without claiming measured
  discovery or protocol behavior.

- Remove the redundant configured copy of the built-in worktree diff check.
  Runtime gate selection now ignores only that exact legacy duplicate, while
  `doctor` reports it and custom diff gates remain untouched.

- Make routine state reads smaller without hiding action-required work:
  `status` defaults to 10 recent jobs and adds uncapped `attention_jobs` plus
  truncation metadata; `hub status --summary` returns a compact per-repo view;
  agent instructions start with `doctor` and request detailed status only when
  needed.

- Base operational recommendations on the latest 20 complete runs while
  retaining selected-history aggregates. `stats.current` discloses the cohort,
  and approval advice now requires a slow median instead of one tail outlier.

- Add `daemon --validate-only` for repeated manual-queue validation. It claims
  no auto-approved jobs, never pushes or verifies, pauses at any existing
  validated train or pending reconcile, and leaves the default and Hub daemons
  auto-deploy-only.

- Replace the product-surface freeze with owner-evidence-gated admission, remove
  stale pre-1.0 lifecycle text from current support documentation, and make the
  release check reject future `SECURITY.md` support-policy drift. Keep that
  checker importable across the supported Python 3.10+ matrix.

## 2.0.0 - 2026-08-29

- Clarify that lease fencing serializes mergetrain runners but cannot stop a
  shell-capable task agent from pushing with its own credential. Document the
  enforceable topology: task agents without integration credentials, a separate
  runner identity, and remote branch protection or a reviewed PR path.

- Remove the no-op `agent.*` settings, presentation-only
  `terminology.git_operation`, `--integrate`/`--push` aliases, and redundant
  `hub list` command. Config and machine-contract versions are now 2; version-1
  configs migrate in memory, while version-2 configs reject removed keys.

- Make cancellation of an in-flight MCP tool coroutine terminate the CLI
  process group instead of leaving validate or an accepted deploy running in a
  detached worker thread. Preserve the existing timeout envelope and
  ambiguous-push reconcile semantics.

- Apply the shared best-effort secret redaction and local-path minimization to
  MCP errors synthesized from malformed child stdout/stderr, while continuing
  to return valid CLI JSON and successful raw log output unchanged.

## 1.4.2 - 2026-08-29

- Resolve relative queue, log, and runner-worktree state to the shared control
  checkout across standard Git linked worktrees. Task worktrees retain their
  own branch/readiness identity while observing one queue and runner lock;
  absolute state paths and relative `--db` overrides keep their existing
  behavior.

- Tighten the generated agent contract around the existing safety boundary:
  task agents enqueue the exact committed HEAD and stop, a task or integration
  request is not deploy approval, and a separately authorized runner may deploy
  only after approval names the displayed exact validated train. No command,
  flag, config field, JSON key, or mutation path is added.

- Add fail-closed, repeatable Codex agent-adoption evidence and a controlled
  launcher, document the supported Google-agent transition from legacy Gemini
  CLI to Antigravity CLI, and make the launcher-path assertion portable to
  Windows shell quoting.

## 1.4.1 - 2026-08-28

- Restore `stats`, history, and Hub observation of an idle WAL queue after its
  last writer removes the `-wal`/`-shm` sidecars. A short `query_only`
  bootstrap initializes SQLite's WAL bookkeeping while the connection returned
  to observers remains strict `mode=ro`.

## 1.4.0 - 2026-08-28

- Persist a privacy-preserving append-only ledger for CLI `reconcile` and
  `recover` invocations, expose operation/apply/outcome counts in `stats`, and
  retain an explicit tracking baseline so pre-upgrade history is reported as
  unknown instead of zero. SQLite schema version is now 11.

## 1.3.1 - 2026-08-28

- Update the pinned PyPI publish action to v1.14.2 so Twine 7 accepts Core
  Metadata 2.5. The v1.3.0 GitHub release built successfully but stopped before
  any PyPI upload; v1.3.1 is the published-package recovery.

## 1.3.0 - 2026-08-28

- Establish a zero-growth-by-default product-surface budget, inventory the
  current CLI, config, dashboard, daemon/Hub, MCP, recovery, notification, and
  validated-reuse surfaces, and classify the core, justified advanced features,
  consolidation candidates, and features that should wait for operating
  evidence. No existing feature is removed.

- Restructure the README around the first-minute product decision: problem,
  fit and non-fit, disposable demo, minimal first run, worktree/hosted-queue
  comparisons, safety guarantees, operating evidence, and links to the full
  technical documentation.

- Replace the custom zero-dependency YAML subset parser with required PyYAML
  `safe_load`, preserving every existing `.mergetrain.yaml` and the stricter
  ambiguous-scalar policy while making full YAML syntax consistent across
  installations. Keep the historical `yaml` package extra as a no-op alias.

- Split SQLite persistence behind the stable `store.py` API into explicit
  transaction, connection, schema, job, lease, claim, event, and recovery
  boundaries. Preserve `BEGIN IMMEDIATE`, WAL/`synchronous=FULL`, migrations,
  token fencing, atomic claims, observer reads, and every schema/JSON contract;
  extend strict typing and coverage floors to the new modules.

- Split the monolithic Git/deploy runner into typed collaborators for managed
  commands, Git/ref primitives, gates, validation reuse, worktrees, and atomic
  push/verification. Keep queue/lease and train orchestration in `GitRunner`,
  preserve CLI/JSON/SQLite behavior, and extend critical coverage floors so
  moving code cannot silently weaken the quality gate.

- Turn measured coverage into a blocking ratchet: require 87% overall across
  the test matrix, apply higher floors to Git/deploy, storage/locking, recovery,
  and validated-reuse modules, and run the same policy in the local deploy gate.
  Enable stricter mypy checks for those correctness-critical modules without
  forcing peripheral CLI and adapter code into strict mode at once.

- Extend `stats` with evidence-backed validation outcomes, validated-to-deployed
  conversion, explicit not-landed and conflict reasons, observed jobs per run,
  and a conservative successful-batch gate-savings estimate. Report
  recovery/reconcile frequency as an evidence gap rather than inferring it from
  mutable job notes.

- Add a force-with-lease-protected `refs/mergetrain/deploys/<sha>` audit ref to
  every atomic deploy. Reconcile now uses that retained remote evidence to
  detect a payload ref that landed and was later force-rewritten, blocking for
  manual recovery instead of requeueing and potentially replaying the deploy.

- Replace the macOS-only `osascript` desktop backend with opt-in browser
  notifications owned by the open dashboard and Hub. The same implementation
  now works in supported macOS and Windows browsers, ignores pre-existing state,
  coordinates duplicate tabs, and focuses the affected repository when an
  alert is clicked. Daemon `--notify` remains available for provider-neutral
  headless webhooks.

- Keep the last good dashboard snapshot visibly marked `DEGRADED` when a live
  snapshot fails, notify once with generic lock-screen-safe copy, and recover
  automatically. Add browser-level coverage for permission handling, duplicate
  tabs, Hub drill-down clicks, degraded recovery, and denied permission. Daemon
  `--notify` now warns when no headless webhook backend is configured. Update
  the dashboard build to Vite 6.4.3 and refresh transitive packages to clear the
  dependency audit.

## 1.2.0 - 2026-07-30

- Normalize checkout line endings before comparing operator configuration with
  the integration-ref blob, avoiding false drift warnings on Windows CRLF
  worktrees.

- Add `supersede`, an atomic validated-train replacement workflow that retires
  the old train without erasing its validation evidence, captures exact clean
  replacement HEADs, records the old/new audit relationship, and never carries
  validation, gate-reuse identity, auto mode, or deploy approval forward
  (#206).

- Add opt-in, resource-bounded parallel pre-push gate groups with explicit
  dependencies, per-gate and plan timeouts, deterministic event/log ordering,
  fail-fast peer process-tree cancellation, and a strictly sequential default
  (#207).

- Give reuse preview and the dashboard one structured eligibility explanation:
  exact identity facts and mismatch causes, truthful per-gate reuse/rerun/skip
  actions, and history-derived savings with sample coverage and confidence.
  Estimates explicitly cannot authorize reuse (#208).

- Attribute queue wait, approval wait, validation/deploy runtime, and runner
  phase latency in `stats`, including retained-history coverage and
  evidence-backed slow-gate recommendations (#203).

- Make `doctor` detect a local operator configuration that differs from the
  locally known integration-ref configuration without fetching or modifying
  the checkout (#204).

- Add an efficient-operation guide for train sizing, gate design, path scopes,
  deterministic environments, persistent-cache and validated-reuse decisions,
  project profiles, and recovery practice (#205).

## 1.1.0 - 2026-07-29

- Add the official MCP Registry `server.json` for #202, including the PyPI
  package identity, exact 1.1.0 version, stdio transport, and fixed `mcp`
  subcommand. Release metadata checks now keep the manifest version, README
  ownership marker, and executable arguments synchronized.

- Match the repository deploy gate to CI's parallel pytest execution (#201).
  The full suite now uses Python 3.12 and pytest-xdist instead of serial
  `unittest` discovery, cutting the measured local gate from roughly five
  minutes to about one while preserving plain `unittest` as the documented
  dependency-light contributor fallback.

- Add the in-repo Claude Code plugin and self-marketplace for #172. The plugin
  bundles the local MCP server, an auto-invocable queue-operation skill, and a
  manual-only deploy skill whose action still passes through the MCP human
  confirmation gate. A CI leg runs Claude Code's strict plugin validator and
  checks the operating skill and repository CLAUDE.md against the
  CLI-generated agent contract, including complete `next_action` and MCP error
  tables. README and LLM guides now include plugin installation and official
  MCP Registry ownership metadata.

- Complete the dashboard history-and-glance follow-up (#178). Running trains
  get a fail-honest ETA from the median of up to 20 recent completed phase and
  gate spans; missing comparable samples show "building history" instead of an
  invented duration. The same read model drives gate waterfall bars. Activity
  gains compact/expanded density and incremental history, phones get a
  state/next-action/attention glance view, and tab titles plus favicons carry
  live state and failure counts. Shared running/validated snapshot fixtures now
  render the actual React components in frontend smoke tests.

- Add an opt-in persistent validation workspace (#200) for build caches whose
  keys include the checkout's absolute path. The stable path is runner-locked,
  hard-resets tracked inputs, preserves only explicitly declared Git-ignored
  cache directories, and invalidates them when the cache key, gate policy, or
  environment fingerprint changes. Deploy and bisect worktrees stay isolated;
  doctor, events, and gc expose the persistent workspace lifecycle.

- Make local validation environment-stable. Pytest now imports the active
  checkout's `src` tree without caller-supplied `PYTHONPATH`, and gate,
  fingerprint, and verify commands prioritize tools installed beside the Python
  interpreter running mergetrain. This prevents stale installed packages and
  unactivated virtualenvs from producing misleading validation failures.

- Add fail-closed path-aware pre-push gates (#199). An optional `gates[].paths`
  list matches repository-relative POSIX globs against the exact assembled
  train diff, including deletions and both rename endpoints. Non-matches emit
  resumable `skipped` gate events; missing or malformed path evidence runs every
  scoped gate. The same policy applies during single/batch validation, exact
  validated reuse, and linear/bisect failure isolation, and participates in the
  validation gate-policy hash.

- Fix plain-text `mergetrain stats` when no `--json` flag is supplied (#197).
  The CLI now reads the aggregate payload's top-level status correctly instead
  of raising `KeyError`, with regression coverage for both empty and populated
  histories.

## 0.9.1 - 2026-07-26

- Add a fail-closed real-remote soak harness for the 1.0 evidence gate. It
  requires an exact disposable-repository sentinel, a clean and idle queue,
  authenticated GitHub access, a post-push verify hook, and an installed wheel
  matching the requested version. Baseline, namespace, recovery events, crash
  truth, and classified interventions persist into a final report.

- Keep repeated conflict exercises unique by including the batch number in the
  contested edit. A second conflict batch could previously reproduce the value
  already on `main`, leaving no change to commit; the regression now proves
  later batches always differ from both their parent and each other.

- Complete the real-repository soak against the published 0.9.0 wheel: 20
  landed trains at a 100% land rate, planned gate-failure and merge-conflict
  recovery, and one real `git push --atomic` SIGKILL whose remote truth matched
  recovery's queued verdict and then deployed through a normal verified train.
  No mergetrain runtime defect was found.

## 0.9.0 - 2026-07-26

- Say so when crash recovery dissolves an approved train (#194). Requeuing a
  stranded row clears its validated-train identity on purpose — a row asserting a
  validation it no longer holds collateral-blocks unrelated auto deploys — but
  the operator retrying a failed deploy then gets whatever is queued now, gated
  together as a new train, which need not be the set they confirmed. The requeue
  now writes that into the job's `note`, naming the dissolved train and saying to
  validate and re-approve; a plain requeued job keeps its plain note, so the
  message stays a signal.

- Name a stranded claim in `next_action`. A job left `in_progress` while no
  runner holds the lock — a crashed runner, or a run that raised after releasing
  its lease — read as an idle queue: `doctor` said `enqueue_clean_branch`, the
  read agents are told to trust. The new `recover_stranded_claim` points at
  `mergetrain recover`, which matters because the next deploy otherwise requeues
  the row automatically and clears its validated-train identity, so a train
  approved by `train_id` can become a different set (retrying with that
  `--train-id` fails closed instead). Additive: a new `next_action` value.

- Stop reporting queue-database contention as the branch's fault (#191). SQLite
  allows one writer, so a process holding the write lock past `busy_timeout`
  made a queue write raise `sqlite3.OperationalError` — not a `MergetrainError`,
  so it fell to the runner's defensive boundary and retired the job terminal
  `failed`, the status that means *fix the branch and enqueue a fresh commit*.
  Nothing had crashed, no ref had moved, and the branch was fine. `immediate()`
  now translates contention into a typed, retryable `QueueBusy`
  (`error.code: queue_busy`), and the runner writes **nothing** on it unless its
  own push is known to have landed — leaving the row exactly as the last durable
  write left it, which is indistinguishable from a crash at the same instant and
  is what `recover_orphans`' marker-aware split already resolves from durable
  evidence. Contention also no longer reads as a toolchain-fingerprint failure in
  the validated-gate reuse check, and `reconcile --apply` no longer reports an
  unwritten decision as `applied: true`.

- Stop inventing a verification failure when the run errored after a landed push.
  The post-push boundary set `verify_status: failed` unconditionally, so a repo
  configured with no verify hooks — whose state was `not_configured` — reported a
  failed verification, sending an operator after a hook that does not exist. And
  when hooks *are* configured but their runner crashed, nothing had determined a
  failure either. Both now record what is true: configured-and-decided outcomes
  are preserved, anything indeterminate becomes `unknown` — the value `doctor`
  turns into `next_action: verify_reconciled_deploy`, so `mergetrain verify`
  discharges it instead of the operator hunting a phantom failure. The completion
  event treats `unknown` with the same `warning` severity `failed` had, so the run
  is not reported as a plain success.

- Add a local fault-injection matrix (`tests/test_fault_*.py`) covering the
  failures that decide whether the queue tells the truth about what shipped: a
  real `git push --atomic` SIGKILLed with the refs applied and without, a push
  that outlives `command_timeout_seconds`, a remote tip moved on top of a landed
  deploy, `unlock --force` stealing the lease between a landed push and its
  terminal write, and SQLite writer contention across both status writes. Every
  existing test of this family faked the push by patching `push_verified_head`,
  so git's real exit code, its real stderr, and the rejection classifier's
  behavior on that stderr were never exercised. `pytest-xdist` is a dev
  dependency and CI runs `-n auto`: the whole suite, fault cases included, takes
  ~30s instead of ~160s, which is what makes injecting these on every push
  affordable.

- Settle key names on the payloads that ship for the first time in this release,
  while they are still free to change: `retry` returns `dismissed_job` (singular,
  an object) so it no longer collides with `dismiss`'s `dismissed` array;
  `history` gate rows report `duration_seconds` like every other finished
  duration, instead of `elapsed_seconds`, which elsewhere means a still-climbing
  counter; `stats` reports flat `median_duration_seconds` and
  `p95_duration_seconds` rather than nesting them under a `duration_seconds`
  object that is a float in `history`; per-gate `states` becomes `state_counts`
  like every other counter map; and `trains.completed` becomes
  `trains.finished`, because it counts deployed + blocked + failed while
  `completed` is configured human vocabulary for the success end state alone —
  and it is `land_rate`'s denominator, so reading it as "shipped" inverted the
  metric.

- Stop reporting a possibly-landed push as `failed`. A non-rejection push
  failure parks the job `needs_reconcile` precisely because the remote may have
  accepted the atomic update, but `push_status` was overwritten with `failed`
  first — the one thing this tool promises not to say. It now stays at the
  `pending` the durable marker already recorded, which is what a crash-orphaned
  job has always carried; reconcile replaces it with the remote's answer. A
  definitive rejection still records `failed`.

- Refuse `run-next` with a push mode while a validated train is pending
  (`error.code: validated_train_pending`). `run-next` claims the next *queued*
  job, so it pushed a different commit and moved the integration ref out from
  under the exact train a human had approved, invalidating that validation
  silently. `docs/cli.md` already directed validated work through
  `run-batch --deploy`; that is now enforced rather than advisory, and
  `agent-contract`'s `boundary.deploy_requires` no longer implies the two
  commands are interchangeable.

- Report a stranded runner as `lease_liveness: "lost"` in event frames, matching
  what `inspect` already said. A one-shot events reader — how the MCP server
  reads progress — had no other lease signal, so an abandoned train was
  indistinguishable from an idle queue.

- Make the fingerprint gate see what it claimed to cover. `keyset()` nulls every
  value, and an empty list pins nothing inside it, so `gc`'s
  `branch_candidates[]` element shape was unpinned — which is how one payload
  shipped `job_id` as a string next to an int `job_id`, and `protected` as the
  string `"true"` in a contract of real booleans. Both are now correct types,
  the `gc` capture seeds a candidate, `gc --apply` is a fingerprinted surface,
  and the JSONL family pins `heartbeat` and both `stream_end` variants instead
  of only `event` and `stream_start` (26 surfaces). New tests pin the
  `error.code` table in `docs/contract.md` against the codes the code can emit,
  and the retryable flag on the recovery path — where `remote_unreachable` is
  retryable, which that table had wrong.

- Point `next_action` at the actual blocker in an unconfigured repository. An
  agent following the mandated read got `enqueue_clean_branch`, and `enqueue`
  then refused with `config_error` — every queue-advancing command does, on
  purpose, rather than shipping against guessed defaults. `doctor` and `status`
  now return the new `initialize_config` value, ranked below the recovery
  actions, which keep working without a config. Additive: a new `next_action`
  value does not bump `contract_version`.

- Close the contract gate's coverage gaps before the 0.9.0 freeze. `init`,
  `run-batch --preview`, `hub add`, `hub list`, and `hub remove` emit
  contract-stamped payloads that no fingerprint covered, so their shapes could
  have changed without CI objecting; the gate now pins 25 surfaces instead of
  20. Regenerating the golden was purely additive — no existing surface changed
  shape, which is the evidence the freeze needs. `docs/contract.md` also
  enumerates the `error.code` vocabulary it tells consumers to branch on,
  including `lock_held` and `remote_unreachable`, which appeared in no document.

- Add `mergetrain mcp`, a stdio Model Context Protocol server behind the
  optional `mcp` extra (#172, phase 1). Every tool shells out to the CLI with
  `--json` and returns that payload verbatim, so `contract_version` stays the
  single machine interface. The surface is deliberately smaller than the CLI:
  `daemon`, `enqueue --auto`, `gc --apply`/`--delete-branches`, `cancel`,
  `unlock`, `dismiss` and the recovery mutations have no tool and no reachable
  parameter, and annotations describe real side effects — `mergetrain_validate`
  is free to run but is not claimed read-only, because it runs gates and moves
  job status. `mergetrain_deploy` takes no `confirm` argument: it re-reads
  doctor/status, refuses to choose between several pending trains, and requires
  a client-rendered human accept, refusing with `confirmation_required` plus the
  terminal command when the client cannot show one. See docs/mcp.md.

- Add `mergetrain demo`, a network-free nine-step walkthrough that creates a
  disposable repo and local bare remote, enqueues four real agent worktrees,
  exposes a two-branch semantic conflict through `conflict_with` — both branches
  green alone and merging cleanly, red only combined — and deploys only the
  validated survivor train. The sandbox isolates user Git config,
  cleans up on success, and is preserved with recovery hints on failure (#171).

- Add read-only `mergetrain history` and `mergetrain stats` commands (#168).
  Durable jobs are grouped into complete trains for status, queue wait,
  duration, land-rate, median/p95 latency, and retained per-gate timing. Queue
  history remains unpruned; payloads disclose the existing 5,000-event gate
  coverage limit instead of silently presenting a truncated tail as complete.

- Add cross-platform, provider-neutral JSON webhooks and single-repo
  `daemon --notify` parity (#167). Notification chains retain the macOS desktop
  backend, filter/deduplicate configured transitions across restarts, and never
  expose credential-bearing webhook URLs in public config or delivery errors.

- Add `mergetrain retry <job-id>` (#166) to atomically replace a fixed
  blocked/failed outcome with a fresh SHA-pinned job while preserving task,
  note, worktree, and auto eligibility. Optional `--rebase` fetches and rebases
  before any queue mutation, so conflicts never dismiss recovery evidence.

## 0.8.1 - 2026-07-23

- Make README images and repository links use absolute GitHub URLs so the
  project description renders correctly on PyPI as well as GitHub.

## 0.8.0 - 2026-07-23

- Reframe the documentation around the integration requirement behind parallel
  agent coding: worktrees provide parallel execution lanes, while one train
  provides the serialized boundary that assembles, proves, and ships their
  combined result. Clarify that this layer preserves end-to-end throughput but
  does not replace task design, meaningful gates, or human-review policy.

- Add a balanced PR-first comparison guide: explain why committed agent
  branches are integration units rather than automatic review units, document
  mergetrain's throughput and combined-validation advantages alongside its
  local-runner/review/governance costs, and describe direct, one-PR, split, and
  validation-only hybrid workflows.

- Preserve remote truth across push and cancellation races (#94). Any
  non-policy push failure after the durable marker is written now parks the
  job in `needs_reconcile` instead of terminal `failed`, because the remote may
  already have accepted the atomic update. A concurrent cancellation request
  is retained for reconcile to honor when no ref landed. An unambiguous remote
  policy/permission rejection still becomes `blocked`, but now clears its DB
  marker and pending pin; successful deploys clear their pins as well. These
  transitions are claim-token/CAS fenced so a stale runner cannot erase newer
  recovery evidence.

- Harden dependency-free config parsing and secret redaction (#95). The built-in
  YAML subset parser rejects unsupported non-empty flow-style collections
  instead of silently treating them as strings, and invalid scalar/container
  types consistently produce `config_error`. Expected errors, persisted job
  notes, `doctor` remote URLs, status JSON, and dashboard snapshots now mask
  passwords embedded in URL userinfo in addition to sensitive assignments and
  command options.

- Expand fail-closed regression coverage for reconcile, recovery/GC, daemon
  TOCTOU guards, and dirty integration cleanup (#96). Consolidate the duplicated
  single/batch marker → push classification → post-push verification sequence
  into one safety path (#97). `status`, `doctor`, and the dashboard now use the
  same config-aware `next_action`, and `dismiss --all` processes every eligible
  blocked/failed row rather than a display-limited subset.

- Close the two post-0.7 adversarial hardening passes across the process, SQLite,
  and Git boundaries (#104–#118, #135–#146). Queue mutations and schema migration
  are fenced against stale owners; runner heartbeats preserve lock identity;
  replaced claims surface as typed lost leases; and registry edits preserve
  forward-compatible fields instead of rewriting data lossily.

- Make ambiguous pushes and crash recovery fail closed. Managed subprocesses
  receive bounded execution context, failed validation fingerprints cannot leak
  side effects into deploys, joint-failure isolation stops after uncertain remote
  state, definitive policy rejections remain distinguishable from ambiguous
  transport failures, and recovery verifies the expected ref pin before pushing.

- Harden every newly audited input boundary: gate commands run through the
  documented POSIX `/bin/sh`; substituted paths are shell-quoted; global CLI
  options honor `--`; status limits reject non-positive values; `init` detects
  scaffold collisions before writing; malformed lock timestamps fail soft; and
  file URLs are decoded exactly once.

- Keep machine-readable observation truthful under failure. Stream consumers now
  receive terminal error frames, pending reconcile ends inspection streams,
  dashboard request parsing rejects malformed and traversal-shaped input, and
  the hub cache preserves config-aware `next_action` state across snapshots.

- Apply one redaction and error taxonomy across CLI output, persisted notes,
  dashboards, and streams. Credential-bearing URL variants are masked, expected
  failures use stable machine codes, and branch resolution accepts qualified refs
  without silently selecting an ambiguous name.

- Raise the release-quality baseline with Ruff, mypy, coverage reporting,
  dedicated race/conflict state-machine tests, installed-wheel smoke tests,
  pinned publishing actions, Dependabot, `SECURITY.md`, and contributor guidance.
  CI continues to block on macOS, Linux, and Windows.

- Add an animated merge-train explainer, a static fallback, and updated recovery,
  configuration, agent-contract, and workflow documentation so the shipped safety
  model and its PR-first tradeoffs are visible before unattended use.

## 0.7.0 - 2026-07-21

- Never gc a live runner's worktree (0.9.0-prep). `gc --apply` listed and
  force-removed **every** mergetrain-prefixed worktree, including the
  integration worktree a running deploy was merging and gating inside, killing
  the run mid-deploy. `gc`, `doctor`, and `recover` now read the live runner
  lock's `worktree_path` and protect it (reported as `active runner worktree,
  skipped`, never removed).

- Unblock the documented first run (0.9.0-prep). mergetrain's own in-repo
  `.mergetrain/` state directory (queue DB, logs, worktrees) was left
  untracked, so the command that created it made the *next* `enqueue` fail the
  clean-worktree check — permanently. The state directory now self-ignores (a
  `.gitignore` of `*` written on first DB open), the dirty-worktree error names
  the offending paths, and `init --write` reports a `next_step` to commit the
  scaffold. A branch that already has a blocked/failed job now refuses
  re-enqueue with a typed `DuplicateActiveBranch` (`error.code:
  duplicate_active_branch`) whose message names the escapes, instead of a
  generic `queue_error` dead-end.

- Add `mergetrain dismiss` so a superseded blocked/failed job can be cleared
  non-destructively (0.9.0-prep). A blocked/failed job never lands and never
  self-clears, so it pinned `doctor`'s `next_action` to `fix_blocked_job`
  forever — hiding a ready validated train — and blocked re-enqueue of its
  branch; the only escape was `cancel`, which the operator docs classify as
  destructive. `dismiss <id>` (or `--all`) moves a blocked/failed job to the
  terminal `canceled` state, and by construction only ever touches
  already-failed outcomes — never queued or in-progress work — so an agent can
  run it unattended. The agent contract, the duplicate-branch error, the
  blocked-job notes, and the failure-modes recipe now point to it. The new
  `--json` surface is fingerprinted.

- Classify a policy-rejected push as `blocked`, not `failed` (0.9.0-prep). When
  the remote refuses the deploy push for a protected branch, a required pull
  request, a denied ref update, or a declined pre-receive hook, the job used to
  land `failed` — which tells an agent "the code is bad, rebase and re-enqueue",
  a wrong and self-perpetuating signal. It now parks `blocked` (a repo-config
  action, not a code fix), and `inspect --json` reports the stable
  `push_rejected` category so agents branch on that instead of regexing the
  note. Transient/infrastructure push failures still mark `failed`
  (`push_failed`). See [failure modes](./docs/failure-modes.md).

- Add `mergetrain verify` to discharge a crash-orphaned post-push verify
  (0.9.0-prep). A crash in the verify window finalizes the job `deployed` with
  `verify_status='unknown'`, and `doctor`'s `next_action` became a **permanent**
  `verify_reconciled_deploy` — no command could clear it, so it masked every
  lower-priority action forever. `mergetrain verify` re-runs the configured
  `deploy.verify` hooks against the recorded `deploy_sha` (assembled in a
  throwaway worktree) and records `succeeded`/`failed`, or takes
  `--ack succeeded|failed` for hooks that can't be re-run. `--job` targets one;
  the default resolves all unresolved. The new `--json` surface is fingerprinted.

- Make the auto-daemon report what actually **landed**, not merely what ran
  (0.9.0-prep). A sweep whose every job blocked on a conflict or failed its
  gates was indistinguishable from a green deploy — `daemon_tick` returned
  `processed:<n>` ("n ran") and the macOS notifier read it as "Train landed
  (n jobs)". Ticks are now graded by outcome: `landed:<n>` (all deployed),
  `partial:<d>/<n>` (some), or `no_landing:<n>` (nothing deployed —
  blocked/failed), and the notifications say so; a repo that keeps landing
  nothing notifies once, like a persistent error, instead of every tick.

- Make the recovery commands honor the contract-1 envelope and widen the
  fingerprint gate (0.9.0-prep). `reconcile`/`recover`/`unlock` returned
  `ok:false` with **no** `error` object when they ran to completion but found
  conflicts (exit 10), no lock (exit 5), or a refused force (exit 4) — using
  `ok` as an outcome grade, the exact thing contract 1 forbids. They now return
  `ok:true` (the command ran; the exit code carries the machine signal),
  `reconcile`/`recover` gain a graded `result` (`success`/`conflict`), and
  `unlock`'s `cleared` bool carries found-or-not — matching the `hub remove`
  precedent. Genuine errors (lock held, remote unreachable, bad config) still
  use the `{ok:false, error:{…}}` envelope. The golden fingerprint gate now
  also watches `recover`, `unlock`, `cancel`, and `hub status`, so their shapes
  can't drift silently before the freeze.

## 0.6.0 - 2026-07-21

- Document the machine contract (#44, Phase 4 — completes #44). New
  `docs/contract.md` enumerates every versioned surface, where
  `contract_version` lives, the contract-1 envelope (`ok`/`result`/`health`/
  the single failure shape), the additive-vs-breaking policy, the too-new
  config handling, and the 0.9.0 freeze linkage. The `agent-contract` payload
  gains a `machine_contract` boundary pointer, and `README`/`llms.txt`/
  `CLAUDE.md` point agents at it.

- Enforce the contract, two ways (#44, Phase 3 — the forcing function that
  makes the 0.9.0 freeze real). A checked-in golden **key-set fingerprint gate**
  (`tests/test_contract_fingerprints.py` + `contract_fingerprints.json`)
  captures the recursive key set of every agent-facing `--json` surface and
  each JSONL frame and fails CI on any un-bumped shape change, classifying it
  as additive (regenerate the golden) or breaking (bump `CONTRACT_VERSION`).
  And a **config preflight**: a `.mergetrain.yaml` whose `version:` is newer
  than this binary understands fails `enqueue`/`run-batch`/`run-next` closed
  with a `config_error` envelope, while `reconcile`/`recover`/`unlock` and all
  read-only commands stay permissive — so a rollback can never lock an operator
  out of crash recovery. `doctor` reports `next_action: upgrade_mergetrain` and
  `config_version_supported` in that state.

- Stamp `contract_version` on every machine-readable surface (#44, Phase 2).
  A single top-level integer (currently 1, from the new `mergetrain.contract`
  module) is injected at the one-shot JSON serializer (`dump_json`), at the
  HTTP `/api/snapshot` boundary (the dashboard-snapshot builder stays bare, so
  a hub payload's embedded per-repo snapshots carry no inner number), and as a
  new `stream_start` header re-emitted at the top of every `events --jsonl`
  stream (the existing `event`/`heartbeat`/`stream_end` frames are unchanged;
  dispatch JSONL on `type`). This is distinct from the product `__version__`,
  so a patch release never reads as a contract change.

- **Contract-1 JSON frame normalization (#44, Phase 1 — a deliberate breaking
  change to the `--json` envelope, made now because it is the last moment
  before the 0.9.0 API freeze).** `ok` now means exactly one thing on every
  command — "the command executed without raising an error envelope" — instead
  of four different things: `doctor`'s repo-health verdict moves to a new
  `health` field (`ok` is now always true when doctor runs); a completed run
  with a post-push verify warning is `ok:true, result:"warning"` (branch on
  `result`, never `ok`, for the outcome); `hub remove` is `ok:true` with the
  existing `removed` bool carrying found-or-not; `agent-contract --json` gains
  `ok:true`. `status --json` now carries `next_action`, so it and `doctor` are
  symmetric (CLAUDE.md tells agents to read either). All three failure shapes
  collapse into one envelope `{ok:false, error:{code,message,retryable},
  next_action?}` — the deploy-reconcile block now reports
  `error.code:"reconcile_pending_deploy"` instead of a bespoke
  `result:"blocked"`/`blocked_reason` shape. Exit codes are unchanged.

- Verify and support Windows (issue #33): the full suite now runs on
  `windows-latest` in CI as a **blocking** check. Fixes a real
  cross-platform bug — `owner_liveness` used `os.kill(pid, 0)`, which on
  Windows is `signal.CTRL_C_EVENT` and sent a real Ctrl-C to the probed
  process instead of checking existence (it would have disrupted the daemon,
  hub, and crash recovery); it now probes via `OpenProcess`/
  `GetExitCodeProcess`. A killed gate/command also returns promptly on
  Windows now (the stdout/stderr drain join is bounded instead of waiting up
  to 10s when `TerminateProcess` leaves a pipe read blocked). The rest of the
  work was test-fixture portability. `docs/install.md` now lists Windows as
  tested.

## 0.5.0 - 2026-07-21

- Harden the 0.4.0 hub after an adversarial review (issues #47–#51): make the
  `--no-daemon` opt-out a real guarantee (samefile registry identity, an
  advisory lock around registry mutations, fail-safe flag parsing, and a
  sweep-level exclusion for aliased duplicate entries); give both daemon loops
  a clean stop (a signal during the inter-sweep wait no longer triggers one
  more deploying sweep) and bound every git subprocess so one hung repo cannot
  starve a whole sweep; keep read-only observation honest (safe sqlite URI
  escaping, no schema migration on an idle sweep, documented WAL sidecar
  limit); stop the snapshot cache serving stale runner liveness / `next_action`
  or pinning stale entries; and fix `hub daemon --notify` dedup so a failed
  delivery is retried, dedup state persists across `--once`/cron runs, and a
  changed error re-notifies.

- Escalate joint-failure isolation from linear to bisect (issue #38): when a
  train of more than 3 jobs fails its gates, the runner now bisects subsets
  (O(log n) gate runs instead of O(n)) to pin the failure. Jobs that fail
  alone finish `failed`; jobs that pass alone but fail together finish
  `blocked` as a named **semantic conflict**, with partner SHAs in the note
  and a new machine-readable `conflict_with` column (schema v7) listing the
  partner job IDs. Surviving jobs are re-run as a fresh train — bisection
  only removes jobs, so nothing ships without a full gate pass over the
  exact final combination. Trains of ≤3 jobs keep the existing one-by-one
  isolation.

- Add `hub daemon --notify` (issue #32 Stage 0): desktop notifications for
  landed trains, sweep errors, and reconcile pauses, deduplicated to state
  transitions so a persistently broken repo notifies once. macOS
  `osascript` only, zero new dependencies; silent no-op elsewhere.

## 0.4.0 - 2026-07-21

- Add a per-repo hub-daemon opt-out: `hub add REPO --no-daemon` keeps a repo
  on the dashboard but excludes it from every `hub daemon` sweep (policy-level
  guarantee for repos that must never see unattended deploys); re-run with
  `--daemon` to re-enable. Excluded repos report the `excluded` outcome and
  show a "daemon off" chip on their card.
- Cache hub snapshots by file fingerprint: a repo's dashboard entry is reused
  while its config and queue database (including the SQLite `-wal`) have
  unchanged mtime/size, replacing a YAML parse plus a database open per repo
  per second with a few `stat` calls. Registry-derived fields (the daemon
  flag) bypass the cache, and error entries are never cached.
- Harden the hub for release: a corrupt or unreadable registry file degrades
  to a visible `registry_error` banner on a live page instead of killing the
  snapshot endpoint and freezing the dashboard; the drill-down hash routes by
  repo path instead of roster index, so removing a repo can no longer switch
  the view to a different repo silently; `llms.txt`/`llms-full.txt` document
  the hub commands and the 0.3.0 `needs_reconcile` recovery contract.
- Add `mergetrain hub status` (RFC #23 Phase 2): one machine-wide read of
  every registered repo's queue — per-repo lines for humans, the hub
  dashboard's aggregate payload with `--json` for coordinator agents.
- Add `mergetrain hub daemon` (RFC #23 Phase 1): the auto-only daemon across
  every registered repo, scheduled machine-wide. Each repo runs through the
  same per-tick policy as the single-repo daemon (only `--auto` jobs, that
  repo's own lock, gates, and reconcile pauses); `--concurrency` caps how
  many repos may run gates simultaneously (default 1, strictly serial), and
  per-repo failures are isolated so a sweep never stops at a broken repo.
- Add `mergetrain hub` (RFC #23 Phase 0): a machine-level repo registry
  (`hub add`/`remove`/`list`) and one read-only multi-repo dashboard with
  per-repo drill-down. The hub owns no queue state — every repo entry is
  read from that repo's own config and SQLite database.
- Add a read-only observer path to queue access (`connect(read_only=True)`):
  no directory creation, no database creation, no schema migration. The hub
  renders a repo with no queue yet as idle and a schema-mismatched or broken
  repo as an isolated error card.

## 0.3.0 - 2026-07-20

- Crash-safe reconciliation and recovery: after any crash, reconcile local queue
  state against the real remote git state and never mislabel a deploy. A durable
  per-job pending-deploy marker (committed `synchronous=FULL` before every push)
  plus a `refs/mergetrain/pending/<id>` pin ref let recovery ask the remote for
  truth — never marking `deployed` unless a push ref carries the sha, never
  re-pushing a landed deploy, never guessing when the remote is unreachable.
- Add `reconcile`, `recover`, and `unlock` commands with a typed exit-code
  contract; a marker-aware orphan split parks a possibly-landed push in the new
  `needs_reconcile` state instead of blindly re-deploying it.
- Hard-block deploy (`run-batch`, `run-next`, and the daemon) while any job
  awaits reconcile; add DB-only `doctor` `next_action` guidance for the new
  states and sweep stale `refs/mergetrain/pending/*` pins during `gc --apply`.

## 0.2.0 - 2026-07-20

- Add opt-in `integrate`/`push` human vocabulary and CLI aliases while keeping
  the `--deploy`, `deployed`, `deploy_sha`, database, and JSON contracts stable.
- Show exact atomic push refspecs in previews and distinguish Git completion
  from downstream provider verification or release.
- Add resumable `events --follow --jsonl` progress with heartbeat and terminal frames.
- Add structured job/train `inspect --json` outcomes and confined `logs --follow` access.
- Keep subprocess output out of structured events while publishing active log paths early.

## 0.1.0 - 2026-07-17

First public alpha release.

- Preserve exact validated train identity for a later approval-gated deploy.
- Rebuild validated trains on the current integration ref and reject changed task HEADs.
- Exclude validated-but-not-deployed branches from destructive GC.
- Fence batch claims and state transitions with unique lease tokens.
- Heartbeat, cancel, and time out long-running Git and shell subprocesses.
- Reject explicitly empty deploy refs and invalid queue timing at config load.
- Return truthful JSON outcomes and non-zero exit codes for blocked/failed jobs.
- Version SQLite migrations with `PRAGMA user_version`.
- Add a loopback-first, read-only live dashboard with SSE and polling fallback.
- Distinguish browser connectivity from runner activity and explain the current gate, command, scope, and Activity milestones.
- Record structured runner phases and explicit lock heartbeat timestamps.
- Redact lease tokens and local filesystem paths from dashboard payloads.
- SQLite-backed local deploy queue.
- Runner lock with PID liveness checks.
- Git worktree merge train execution.
- Configurable pre-push gates and post-push verify hooks.
- Atomic push refs.
- Auto-only daemon boundary.
- JSON-first agent contract, status, doctor, and GC output.
