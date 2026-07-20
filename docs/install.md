# Install

## From PyPI

```sh
python -m pip install mergetrain
```

## Platforms

Developed and continuously tested on **macOS and Linux** (Python 3.10–3.14).
**Windows is untested** — the code is pure Python, but queue locking, worktree
paths, and subprocess handling have not been verified there; see the
[tracking issue](https://github.com/yongjip/mergetrain/issues/33) before
relying on it.

## Local editable install

```sh
python -m pip install -e .
```

## Optional YAML dependency

`mergetrain` has no required runtime dependencies. If you want full YAML parsing
instead of the built-in generated-config subset parser:

```sh
python -m pip install 'mergetrain[yaml]'
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
