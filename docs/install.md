# Install

## From PyPI

mergetrain is a machine-level CLI (one hub and daemon serve every repo), so a
global tool install is the natural fit:

```sh
uv tool install mergetrain      # recommended
pipx install mergetrain         # equivalent alternative
```

Try it without installing anything:

```sh
uvx mergetrain --help
```

On macOS, Homebrew works without any Python on your side (brew brings its
own and installs into an isolated environment):

```sh
brew install yongjip/tap/mergetrain
```

The [tap](https://github.com/yongjip/homebrew-tap) tracks PyPI releases
automatically via a daily bump workflow.

Inside an existing virtual environment, plain pip works too:

```sh
python -m pip install mergetrain
```

> [!NOTE]
> On Homebrew/Debian-managed Pythons, a bare `pip install` outside a
> virtualenv is rejected with an `externally-managed-environment` error
> (PEP 668). Use `uv tool install` or `pipx install` there — that is exactly
> what they are for.

## Platforms

Continuously tested on **macOS and Linux** (Python 3.10–3.14) and on
**Windows** (Python 3.13) — the full suite runs on `windows-latest` in CI as a
blocking check, covering queue locking, worktree paths, and subprocess
handling. Real-world Windows reports (including "it just worked") are still
welcome on the [tracking issue](https://github.com/yongjip/mergetrain/issues/33),
since a clean CI runner cannot exercise every local Git configuration.

## Local editable install

```sh
python -m pip install -e .
```

## Codex native plugin

With Codex CLI and `uv` installed, add the repository's Git marketplace and
install the plugin:

```sh
codex plugin marketplace add yongjip/mergetrain --ref main
codex plugin add mergetrain@mergetrain
```

Adding the marketplace makes the problem-first listing available in Codex;
installing the plugin loads its skill and the same five-tool, release-pinned
stdio MCP server. The ordinary agent path remains `status → enqueue → stop`.
The plugin does not grant deployment, unattended-operation, or recovery
authority.

## Claude Code plugin

With Claude Code and `uv` installed, add the repository marketplace and install
the plugin:

```sh
claude plugin marketplace add yongjip/mergetrain
claude plugin install mergetrain@mergetrain
```

The same commands are available interactively as `/plugin marketplace add` and
`/plugin install`. Installing the plugin loads its problem-first skill and the
same five-tool, release-pinned stdio MCP server. The ordinary agent path remains
`status → enqueue → stop`; the plugin does not grant deployment,
unattended-operation, or recovery authority.

## Pin a version and upgrade deliberately

The runner, its daemon, and every script or agent that reads mergetrain's JSON
should use the same release, and moving to a new one should be a decision, not
a side effect of a routine `brew upgrade`. Pin the version where the runner
gets it:

```sh
uv tool install mergetrain==3.4.1    # or: pipx install mergetrain==3.4.1
brew pin mergetrain                  # Homebrew: keep `brew upgrade` from moving it
```

- A container image can take the version as a build argument and install
  `mergetrain==${MERGETRAIN_VERSION}`.
- If the repository has a Python development environment that the runner's
  gates already use, pin mergetrain there too, so every worktree that syncs it
  resolves the same release: for example `mergetrain==3.4.1` in its
  `requirements.txt` or in the `dev` extra of its `pyproject.toml`. GitHub's
  dependency graph reads those files, so the repository also appears among
  mergetrain's dependents.

To upgrade, use [the upgrade procedure](upgrading.md) to adapt consumers and
replace running processes while preserving existing work:

1. Read every [changelog](../CHANGELOG.md) section between the pinned version
   and the target, starting with each release's Upgrade notes. Identify the
   commands, fields, contract assertions, and safety actions each script reads.
   Future contract changes require a major release; the published 3.3.x-to-3.4.x
   contract 4-to-5 migration still needs consumer adaptation.
2. Check changed exit codes as well as JSON fields. Since 3.4.0 `daemon --once`
   exits 1 when a tick fails or processed jobs do not all land (or validate with
   `--validate-only`); an idle tick still exits 0.
3. Adapt and test consumers to accept both the current and target contracts
   using an explicit reviewed set, while keeping their existing safety checks.
   Test with the current CLI, then land the consumer fix through the current
   authorized runner **before changing the installation pin**. A completion
   wrapper that must land its own fix through its queue needs this order. If
   it already blocks the fix, use the guide's
   [bootstrap procedure](upgrading.md#land-compatibility-fixes-before-changing-the-runner).
4. Inventory the CLI, live daemon, Hub daemon, and MCP separately. Let active
   ticks finish and confirm no managed repository has a running job before
   changing the environment. Move the pin and restart the previously authorized
   daemon and Hub at that idle boundary using the verified target executable;
   queued and Ready work can remain. Preserve their launch arguments and scope.
   A pin inside the repository lands through the train like any other change.
   Confirm the release, contract, and actual new processes; `status --diagnose`
   reports the querying CLI's version, not a live daemon's.

An agent asked to upgrade keeps the target release and repairs affected
consumers within the authorized scope. A **Breaking** label or unknown
contract number calls for migration review, not an automatic rollback or a
blanket stop. Preserve unknown-safety-action refusals and existing deployment
boundaries; report a concrete remaining incompatibility when it cannot be
resolved. See [the rollback boundary](upgrading.md#distinguish-compatibility-repair-from-rollback).

## Updating the plugins

The Claude Code and Codex plugins pin their MCP package to one release.
Publishing a new version to PyPI or upgrading the global `mergetrain` CLI does
not replace an installed plugin or its running MCP server. Update the plugin
and start a new session to load its new release pin.

These commands are for the `mergetrain` marketplace installed above. If you
installed from another catalog, substitute its marketplace name.

### Codex

Refresh the Git marketplace, install the plugin from the refreshed source,
and inspect the installed version:

```sh
codex plugin marketplace upgrade mergetrain
codex plugin add mergetrain@mergetrain
codex plugin list --marketplace mergetrain --json
```

Check the `installed` entry for `mergetrain@mergetrain`: `version` should match
the release you intend to use, and `enabled` should be `true` to use its tools.
Start a new Codex session after updating; restart the desktop app if it still
loads the previous plugin. Marketplace refresh can update configured plugin
files, but publishing a release does not establish when every user's client
will refresh. See [the official OpenAI marketplace guidance](https://developers.openai.com/plugins/build/plugins#add-a-marketplace-from-the-cli).

### Claude Code

Third-party marketplaces such as `mergetrain` have auto-update off by default.
To enable it, open `/plugin`, select **Marketplaces → mergetrain → Enable
auto-update**. For a manual update of the default user-scoped installation:

```sh
claude plugin marketplace update mergetrain
claude plugin update mergetrain@mergetrain
claude plugin list --json
```

If you installed at project or local scope, add the matching `--scope project`
or `--scope local` to `claude plugin update`. Confirm the installed mergetrain
version in the list, then restart Claude Code. Versions that support
`/reload-plugins` can apply the update in an existing session; otherwise that
session keeps its previously loaded version. See [Claude's update policy](https://code.claude.com/docs/en/discover-plugins#keep-plugins-updated).

Both clients follow the marketplace's configured Git ref. A fixed tag or
commit stays fixed when refreshed; use `main` to follow new releases. The
global `mergetrain --version` reports a separate CLI installation, so use the
plugin lists above to check plugin versions and the client's MCP view to
confirm its mergetrain server connects.

## agy native plugin

With [Antigravity CLI](https://www.agy.dev/docs/cli/plugins/) and `uv` already
installed, add the repository as a native plugin:

```sh
agy plugin install https://github.com/yongjip/mergetrain
```

The root `plugin.json` supplies the problem-first skill and `mcp_config.json`
launches the release-pinned `mergetrain[mcp]` package through `uvx`. The first
tool use may download that wheel. No hosted service or provider credential is
introduced.

The agent's normal path remains `status → enqueue → stop`. The plugin does not
grant deploy, unattended, recovery, force-unlock, or cleanup authority; if the
client cannot render MCP deployment confirmation, it reports the ordinary
terminal command and stops.

Validate a source checkout before installing it:

```sh
agy plugin validate .
```

## Config parser dependency

PyYAML is installed automatically and `.mergetrain.yaml` is always read with
its safe loader. Existing install commands that select the historical `yaml`
extra remain valid, but the extra is now a no-op compatibility alias:

```sh
uv tool install 'mergetrain[yaml]'
python -m pip install 'mergetrain[yaml]'
```

New installs should simply use `mergetrain` without the extra.

## Verify installation

```sh
mergetrain --version
mergetrain status --diagnose --json
```

`--version` is the stable one-line compatibility check. Diagnostic status also
identifies the imported package path, wheel/editable install mode, and Git
commit/dirty state when those facts can be discovered safely. This is useful
for detecting a stale editable install that has the same semantic version as a
released wheel.

## From source without installing

```sh
PYTHONPATH=src python -m mergetrain --version
PYTHONPATH=src python -m mergetrain status --diagnose --json
```
