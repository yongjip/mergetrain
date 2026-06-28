# Install

## Local editable install

```sh
python -m pip install -e .
```

## Optional YAML dependency

`trainyard` has no required runtime dependencies. If you want full YAML parsing
instead of the built-in generated-config subset parser:

```sh
python -m pip install -e '.[yaml]'
```

## Verify installation

```sh
trainyard --version
trainyard agent-contract --json
```

## From source without installing

```sh
PYTHONPATH=src python -m trainyard --version
PYTHONPATH=src python -m trainyard doctor --json
```
