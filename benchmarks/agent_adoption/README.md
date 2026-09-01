# Agent adoption harness

This repository-local harness implements the first reproducible slice of the
[agent adoption benchmark](../../docs/agent-adoption-benchmark.md). It is not
installed with the `mergetrain` wheel and adds no product CLI or telemetry.

Diagnostic pilot notes live under [`pilots/`](pilots/) and explicitly separate
scored trials from launcher or instrumentation failures. The current repeated
Codex result after the 1.4.2 corrections is recorded in the
[`2026-08-29` note](pilots/2026-08-29-codex-142-current-init-repetitions.md).
The first Antigravity CLI operational cell is recorded separately in the
[`2026-09-01` note](pilots/2026-09-01-agy-current-init-repetitions.md).

The current milestone supports the `current_init` condition and one Tier-1 task.
It creates a disposable Git repository, local bare remote, task worktree, hidden
task check, command wrappers, and immutable result record. The wrappers record
agent calls to `git` and `mergetrain`; calls made by mergetrain itself are marked
separately.

Prepare an absent run directory:

```sh
python -m benchmarks.agent_adoption.harness prepare \
  --run-dir /tmp/mt-adoption-run \
  --condition current_init \
  --mergetrain /opt/homebrew/bin/mergetrain
```

The command prints the task worktree and writes `prompt.txt`. Run an agent under
the tracing boundary by placing its ordinary command after `--`:

```sh
python -m benchmarks.agent_adoption.harness run \
  --run-dir /tmp/mt-adoption-run \
  --agent-product codex \
  --agent-version "$AGENT_VERSION" \
  --model "$MODEL_ID" \
  --reasoning-setting "$REASONING_SETTING" \
  --permission-profile "$PERMISSION_PROFILE" \
  -- codex exec "$(cat /tmp/mt-adoption-run/prompt.txt)"
```

Set those four environment variables to the exact values used for the trial;
the harness rejects missing or empty agent provenance rather than silently
pooling incomparable runs.

Then finalize exactly once:

```sh
python -m benchmarks.agent_adoption.harness finalize \
  --run-dir /tmp/mt-adoption-run
```

`finalize` writes `result.json`, validates its required contract, and exits `0`
only for a safe autonomous handoff. A behavioral failure exits `1`; a harness
or input error exits `2`. It never runs a merge, validation, deploy, or network
remote. The fixture's `origin` is a local bare repository.

The agent launcher must preserve the harness-injected `PATH` and
`MERGETRAIN_BENCHMARK_*` environment variables for its spawned shell commands.
If queue state proves that mergetrain ran but the command boundary has no
matching trace, the grader records `harness_error` and excludes the run from
behavioral interpretation instead of guessing.

On macOS, Codex invokes login `zsh`, whose system profile can reorder `PATH`.
Use the repository's controlled benchmark adapter so traced `git` and
`mergetrain` remain first after login-shell initialization:

```sh
python -m benchmarks.agent_adoption.harness run \
  --run-dir /tmp/mt-adoption-run \
  --agent-product codex \
  --agent-version 0.150.1 \
  --model gpt-5.6-sol \
  --reasoning-setting max \
  --permission-profile 'approve-for-me(workspace-write); add-dir=control; shell-env=core+explicit-trace+controlled-zprofile; shell-network=disabled; ignore-user-config; ephemeral' \
  -- python benchmarks/agent_adoption/codex_launcher.py \
    /tmp/mt-adoption-run/prompt.txt \
    /tmp/mt-adoption-run/control
```

The adapter changes only the observation boundary: it uses an ephemeral
`ZDOTDIR`, restores the harness wrapper directory at the front of `PATH`, passes
the required trace variables explicitly, disables shell-command network access,
and starts a fresh ephemeral Codex session with user config ignored. Use
`--codex`, `--model`, or `--reasoning` only when the corresponding provenance
fields are changed too. Other agent products need their own equally explicit
benchmark adapters; they do not belong in mergetrain core.

Antigravity CLI also needs a product-specific observation adapter. Authenticate
interactively once, then use a fresh project and the exact model and permission
profile recorded for the trial:

```sh
python -m benchmarks.agent_adoption.harness run \
  --run-dir /tmp/mt-adoption-run \
  --agent-product antigravity-cli \
  --agent-version 1.1.22 \
  --model gemini-3.1-pro-high \
  --reasoning-setting high \
  --permission-profile 'fresh-project+print; tool-permission=proceed-in-sandbox; mode=accept-edits; sandbox; add-dir=task+control; allow=scoped-unsandboxed; slash-commands=disabled; permission-bypass=off' \
  -- python benchmarks/agent_adoption/agy_launcher.py \
    /tmp/mt-adoption-run/prompt.txt \
    /tmp/mt-adoption-run/control \
    --agy /path/to/agy \
    --model gemini-3.1-pro-high \
    --effort high
```

The adapter starts a new headless project, emits `stream-json`, disables slash
commands, enables the terminal sandbox, explicitly adds both the task and
control repositories to the workspace, and never uses
`--dangerously-skip-permissions`. It also restores tracing-wrapper precedence
through a controlled `ZDOTDIR` on macOS.

Headless Ask decisions are soft-denied, while Antigravity CLI `1.1.22` on the
measured macOS host could not read a linked-worktree current directory from its
native terminal sandbox. The measured fallback therefore used only these
temporary global Allow rules:

```text
unsandboxed(git)
unsandboxed(mergetrain)
unsandboxed(python3)
unsandboxed(pytest)
unsandboxed(ls)
unsandboxed(PYTHONPATH=.* python3)
```

These rules are stored in the user's global Antigravity settings, not the
disposable run. Remove them immediately after the benchmark. Do not broaden
them to `unsandboxed(*)`; the official
[headless](https://antigravity.google/docs/cli/headless/) and
[permissions](https://antigravity.google/docs/cli/permissions/) documentation
recommends scoped rules instead of the all-tools bypass.

The `current_init` condition intentionally preserves the released `init --write`
output. If an agent enqueues into task-worktree-local state instead of the
control repository's shared queue, the grader records `wrong_queue`; the harness
does not redirect the command and hide the onboarding failure.

Artifacts are local by default:

```text
RUN/
  manifest.json
  prompt.txt
  control/                 # initialized repository and shared queue
  task/                    # agent's task branch/worktree
  remote.git/              # local-only origin
  grader/check_task.py     # hidden deterministic task check
  bin/                     # platform-native tracing wrappers used only by `run`
  codex-zdotdir/           # adapter-owned login-shell profile, when Codex is used
  agy-zdotdir/             # adapter-owned login-shell profile, when AGY is used
  artifacts/
    trace.jsonl
    remote-updates.jsonl
    agent.stdout
    agent.stderr
    agent-run.json
  result.json              # immutable final record
```

Do not publish raw transcripts or paths without redaction. Each changed fixture,
prompt, generated protocol, Skill, or tool manifest requires a new condition or
fixture revision in the result record.
