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

## Optional YAML dependency

`mergetrain` has no required runtime dependencies. If you want full YAML parsing
instead of the built-in generated-config subset parser:

```sh
uv tool install 'mergetrain[yaml]'      # or: pipx install 'mergetrain[yaml]'
python -m pip install 'mergetrain[yaml]'  # inside a virtualenv
```

For an editable checkout with the same extra, use
`python -m pip install -e '.[yaml]'`.

## Verify installation

```sh
mergetrain --version
mergetrain version --json
mergetrain agent-contract --json
```

`--version` remains a stable one-line compatibility check. `version --json`
also identifies the imported package path, wheel/editable install mode, and Git
commit/dirty state when those facts can be discovered safely. This is useful for
detecting a stale editable install that has the same semantic version as a
released wheel.

## From source without installing

```sh
PYTHONPATH=src python -m mergetrain --version
PYTHONPATH=src python -m mergetrain doctor --json
```
