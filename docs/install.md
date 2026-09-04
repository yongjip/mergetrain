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
