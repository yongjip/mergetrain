# Install

## Local editable install

```sh
python -m pip install -e .
```

## Optional YAML dependency

`mergetrain` has no required runtime dependencies. If you want full YAML parsing
instead of the built-in generated-config subset parser:

```sh
python -m pip install -e '.[yaml]'
```

## Verify installation

```sh
mergetrain --version
mergetrain agent-contract --json
```

## From source without installing

```sh
PYTHONPATH=src python -m mergetrain --version
PYTHONPATH=src python -m mergetrain doctor --json
```
