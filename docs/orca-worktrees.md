# Use mergetrain from Orca worktrees

Orca can create task worktrees and run their terminals. Mergetrain accepts the
committed branches from those worktrees, shares one queue per Git repository,
and checks the assembled train. The terminal workflow uses existing
mergetrain commands; it does not require the optional Orca queue panel.

## Set up the repository before creating tasks

Install mergetrain **3.7.1** on the host that owns the checkout. The recorded
Orca workflow tests used **3.5.0**; the target keeps machine contract **5** and
config schema **2**. See
[installation](install.md) for a pinned install and [upgrading](upgrading.md)
before changing an existing runner.

Use the control checkout: the checkout that holds the shared Git directory.
For a normal clone this is the original checkout, even when Orca displays a
linked task worktree as the project. In its Orca terminal:

```sh
mergetrain --version
mergetrain --repo "/path/to/example-app" status --json
mergetrain --repo "/path/to/example-app" init --project example-app --write
```

Initialize only an unconfigured repository. `init --write` refuses to replace
existing files. For an existing setup, inspect it with
`status --diagnose --json` and follow its `next_action` instead.

Add a real project gate to `.mergetrain.yaml`. For a project whose tests use
Python's standard-library `unittest`, this is a complete example:

```yaml
version: 2
project:
  name: example-app
gates:
  - name: tests
    run: '"$MERGETRAIN_RUNNER_PYTHON" -B -m unittest discover -s tests'
```

Use your project's actual checks. The example uses the Python interpreter
running mergetrain and creates no bytecode. A fresh `init` has `gates: []` and
therefore runs no project tests. If your remote or integration branch differs
from the defaults `origin` and `main`, set the matching values described in
[configuration](config.md).

Link `AGENTS.mergetrain.md` or `CLAUDE.mergetrain.md` from the corresponding
agent instructions, then commit the setup:

```sh
git add .mergetrain.yaml AGENTS.mergetrain.md CLAUDE.mergetrain.md
git commit -m "Configure mergetrain checks and agent handoff"
```

Land the setup through your normal integration process before creating task
worktrees. The control checkout must contain the landed configuration.
Mergetrain reads that checkout's policy, not the task branch's copy. Linked
worktrees then share its queue DB, logs, and runner lock automatically. Do not
create a separate config or DB for each Orca task.

Gates run in an isolated assembled checkout. Orca's task setup hook does not
prepare that checkout's dependencies. Make dependency preparation part of the
project's gate or configured cache policy. Gates must leave tracked files
unchanged and generated files ignored or removed. A symlink such as `dist`
can be an untracked file even when a `dist/` directory would be ignored. See
[keeping the assembled tree clean](best-practices.md#keep-the-assembled-tree-clean).

## Create and identify the Orca worktree

Add the Git repository to Orca and create a task worktree from the configured
integration branch. The following uses Orca's CLI; replace `REPO_ID` with the
repository's returned `id`:

```sh
orca repo list --json
orca worktree create --repo "id:REPO_ID" --name feature-a --base-branch main --no-parent --json
orca worktree list --repo "id:REPO_ID" --json
```

Use the executable belonging to your Orca session: `ORCA_CLI_COMMAND` when
provided, `orca-dev` for a dev build, or `orca` for an installed macOS/Windows
build. On Linux outside an Orca terminal, use `orca-ide`; bare `orca` can refer
to the GNOME screen reader. Load the matching guide with
`orca skills get orca-cli --json`, substituting that executable too.

`--base-branch main` chooses the Git base; change it if your integration branch
is different. `--no-parent` only makes this task independent in Orca's workspace
lineage. It does not create another mergetrain queue. Orca's repository setup
policy still applies.

Copy the returned worktree `id` and `path`; do not infer them from the display
name. For Orca commands, use the full worktree selector `id:REPO_ID::PATH`.
For mergetrain, pass only the filesystem `path` to `--repo`. In particular,
macOS `/var` and `/private/var` aliases can differ from Orca's stored path;
using the exact returned ID avoids a `selector_not_found` refusal.

Open the worktree's terminal and confirm its actual branch:

```sh
git branch --show-current
git status --short
```

Orca's generated branch name can differ from the display name. The commands
below use `TASK_BRANCH` as a placeholder for the branch reported by Git and
`/path/to/feature-a` for the returned worktree path. Quote paths containing
spaces. Keep a clean, committed task branch before registering it.

## Register committed work, then validate when authorized

From the task worktree's Orca terminal:

```sh
mergetrain --repo "/path/to/feature-a" status --json
mergetrain --repo "/path/to/feature-a" enqueue --task "add feature a" --branch "TASK_BRANCH" --json
```

Mergetrain verifies the worktree and pins its current head and integration
base. It refuses uncommitted work and another active job on the same branch.
A task agent stops after successful registration unless validation was also
explicitly authorized.

The operator can read the same queue from the control checkout:

```sh
mergetrain --repo "/path/to/example-app" status --json
```

When validation is authorized and `next_action` recommends
`validate_queued_jobs`, run it from either checkout on that execution host:

```sh
mergetrain --repo "/path/to/feature-a" validate --json
mergetrain --repo "/path/to/example-app" status --json
```

`validate` assembles queued branches and runs their combined gates. It never
pushes. Read both the execution `result` and the subsequent status; `ok: true`
alone does not mean every gate passed. A partial or failed validation exits
`1` while still returning structured job results.

| Observation | Meaning and next step |
| --- | --- |
| Waiting | Registered work is awaiting an authorized runner. |
| Running | A runner owns the queue; observe it rather than start another. |
| Ready | One exact train passed validation. This guide stops here; deployment requires separate authorization. |
| Attention | Read the job's reason and `next_action`; inspect that job when evidence is needed. |
| `health: unconfigured` or `degraded` | Complete or repair host/repository setup before mutation. |

All task worktrees can read the shared result. Orca workspace grouping does
not isolate the queue. If a Ready train already exists, validation pauses;
resolve that existing train through the operator's authorized workflow before
validating later work.

## Repair a failed gate

Start with `status --json`, then inspect the failed or blocked job named by
`next_action.target_job_id`. Replace `JOB_ID` below with that number:

```sh
mergetrain --repo "/path/to/feature-a" inspect JOB_ID --json
```

Fix the owning task branch and commit a clean result. Use the existing retry
path, rather than enqueueing the same active branch again:

```sh
git add .
git commit -m "Fix the failed project check"
mergetrain --repo "/path/to/feature-a" retry JOB_ID --json
mergetrain --repo "/path/to/feature-a" status --json
```

Retry accepts only a failed or blocked job. It replaces that outcome with a
new job and freshly captured commits; it does not run validation. If validation
is authorized, follow the new status recommendation with `validate --json`.
Successful repair produces Ready with the repaired head, while local and
remote integration refs remain unchanged.

## Codex, Claude Code, and agy

See the [Orca agent guide](orca-agents.md) for native plugin installation,
client-specific checks, and task/validation prompt examples tested from real
Orca terminals.

## Optional Orca Source Control panel

An experimental queue panel is available in
[Orca draft PR #1](https://github.com/yongjip/orca/pull/1). This is a tested dev
build, not a claim that the panel is in a released Orca version. The terminal
commands above do not depend on it.

In a build containing that change:

1. Select the task worktree and expand **Mergetrain** in Source Control.
2. Choose **Register branch**, enter a task description, and choose **Add to queue**.
3. When authorized, choose **Validate queue**. Switching to another linked
   worktree preserves the shared operation and result.
4. Read Ready/Attention and any failure reason. For repair, commit the fix and
   run `retry` in the task terminal, then **Refresh** and validate again.

The panel supports contract 5 and needs mergetrain on the execution host's
PATH. Deployment remains in mergetrain's separately authorized operator flow.

## Verification and host limits

Before writing this guide, the following real flows passed on **2026-10-14**:

- An Orca-created worktree and real Orca PTYs ran initialization, committed
  setup, registration, status from both checkouts, and validation. Paths
  contained spaces. Dirty and duplicate registration were refused.
- Orca UI registered a failing branch, displayed its gate failure, then showed
  Ready after a fix and freshly pinned retry executed in its real terminal.
- Two linked worktrees were registered through the UI; switching during
  validation retained the operation, with one Ready and one Attention result.
- Every flow checked exact task/base commits, `auto_deploy: false`,
  `push_status: not_run`, and unchanged local/remote main after validation.

The three Electron end-to-end tests used hidden renderers, isolated profiles,
a local bare Git remote, mergetrain 3.5.0, and Orca dev 1.4.214 at commit
`b6ab6b700233abca5a867f7b3cfe4f8320b78fd7`. A preceding run also validated the
published Orca PR branch with desktop/E2E type checks, 18 integration unit
tests, and changed-code quality. See the
[recorded verification summary](evidence/orca-worktrees-2026-10-14.json).

Windows, WSL, and SSH were not exercised together with Orca in these local
runs. Install mergetrain and gate tools on the workspace's execution host and
use paths from that host. Do not run a local checkout as a substitute for an
unreachable remote. Mergetrain gates use POSIX `sh`, including Git for Windows'
`sh.exe`; adapt interactive terminal examples to your shell. Folder-only
workspaces need a Git repository before this worktree queue workflow applies.
