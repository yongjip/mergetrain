# Agent adoption harness

This repository-local harness implements the first reproducible slice of the
[agent adoption benchmark](../../docs/agent-adoption-benchmark.md). It is not
installed with the `mergetrain` wheel and adds no product CLI or telemetry.

Diagnostic pilot notes live under [`pilots/`](pilots/) and explicitly separate
scored trials from launcher or instrumentation failures.

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
