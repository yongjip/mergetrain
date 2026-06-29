# Release checklist

1. Run tests.

   ```sh
   PYTHONPATH=src python -m pytest -q
   # or, dependency-free: PYTHONPATH=src python -m unittest discover -s tests
   ```

2. Verify CLI contract.

   ```sh
   PYTHONPATH=src python -m mergetrain agent-contract --json
   ```

3. Build package.

   ```sh
   python -m build
   ```

4. Install wheel in a clean environment.

   ```sh
   python -m pip install dist/*.whl
   mergetrain --version
   ```

5. Cut the release.

   ```sh
   git tag v0.1.0
   git push origin v0.1.0
   ```

   Then publish a GitHub Release for the tag. That triggers
   `.github/workflows/release.yml`, which builds and uploads to PyPI.

6. Release note highlights.

   - SQLite-backed local deploy queue.
   - Runner lock with a refreshed lease (stale leases reclaim safely).
   - Git worktree merge trains.
   - Configurable gates and atomic push refs.
   - Auto-only daemon.
   - JSON doctor/status/agent-contract.

## Publishing to PyPI

### One-time: configure Trusted Publishing (no API token)

On <https://pypi.org> → your account → **Publishing** → add a **pending
publisher** with exactly:

| Field | Value |
| --- | --- |
| PyPI project name | `mergetrain` |
| Owner | `yongjip` |
| Repository name | `mergetrain` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

Then in the GitHub repo create an **Environment** named `pypi`
(Settings → Environments). No secrets are needed — auth is OIDC.

### Rehearse on TestPyPI first (recommended)

```sh
python -m build
python -m twine upload -r testpypi dist/*
python -m pip install -i https://test.pypi.org/simple/ mergetrain
```

### Production publish

Publishing a GitHub Release runs the release workflow and uploads the built
sdist + wheel to PyPI automatically. A given version can only be uploaded once;
bump `project.version` in `pyproject.toml` for each release.
