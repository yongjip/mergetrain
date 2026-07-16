# Release checklist

PyPI releases are built by GitHub Actions and published with short-lived OIDC
credentials. Do not upload production artifacts from a developer machine.

## What CI verifies

Every pull request runs:

- unit tests on macOS and Linux with Python 3.10 through 3.14;
- the installed-CLI E2E suite on macOS and Linux;
- version and changelog consistency checks;
- isolated sdist and wheel builds;
- `twine check --strict` on both distributions; and
- a clean-environment wheel install and CLI smoke test.

The same metadata, unit, build, and strict package checks run again from the
release tag before any job receives PyPI credentials.

Useful local equivalents:

```sh
PYTHONPATH=src python -m unittest discover -s tests
PYTHON=python3.12 bash scripts/e2e.sh
python scripts/check_release.py --tag v0.1.0
python -m build
python -m twine check --strict dist/*
```

## One-time Trusted Publishing setup

Create two GitHub Environments in repository settings:

| Environment | Purpose | Recommended protection |
| --- | --- | --- |
| `testpypi` | Manual TestPyPI rehearsal | Required reviewer; restrict to `main` |
| `pypi` | Production PyPI release | Required reviewer; restrict to protected tags |

Then register one pending publisher on each package index. The values must
match exactly.

### TestPyPI

On <https://test.pypi.org/manage/account/publishing/>:

| Field | Value |
| --- | --- |
| Project name | `mergetrain` |
| Owner | `yongjip` |
| Repository | `mergetrain` |
| Workflow | `test-release.yml` |
| Environment | `testpypi` |

### Production PyPI

On <https://pypi.org/manage/account/publishing/>:

| Field | Value |
| --- | --- |
| Project name | `mergetrain` |
| Owner | `yongjip` |
| Repository | `mergetrain` |
| Workflow | `release.yml` |
| Environment | `pypi` |

No GitHub or PyPI API token is stored in repository secrets. Protect both
accounts with 2FA. Environment approval is the final human release boundary.

## Rehearse on TestPyPI

After the release-preparation pull request is merged:

1. Open **Actions → TestPyPI → Run workflow** on `main`.
2. Approve the `testpypi` environment deployment.
3. Wait for the publish job to complete.
4. Install the exact version from TestPyPI in a fresh environment:

   ```sh
   python -m venv /tmp/mergetrain-testpypi
   /tmp/mergetrain-testpypi/bin/python -m pip install \
     --index-url https://test.pypi.org/simple/ --no-deps mergetrain==0.1.0
   /tmp/mergetrain-testpypi/bin/mergetrain --version
   /tmp/mergetrain-testpypi/bin/mergetrain dashboard --help
   ```

Package versions are immutable on each index. Bump the version before repeating
an upload that already succeeded.

## Publish to production

1. Confirm the TestPyPI rehearsal and all `main` CI checks passed.
2. Update the version and dated changelog heading for the intended release.
3. Create an annotated tag on the exact verified `main` commit and push it:

   ```sh
   git switch main
   git pull --ff-only
   python scripts/check_release.py --tag v0.1.0
   git tag -a v0.1.0 -m "mergetrain 0.1.0"
   git push origin v0.1.0
   ```

4. Publish a GitHub Release for that existing tag:

   ```sh
   gh release create v0.1.0 --verify-tag --generate-notes \
     --title "mergetrain 0.1.0"
   ```

5. Approve the `pypi` environment deployment after the unprivileged build job
   passes. Publishing the GitHub Release triggers `.github/workflows/release.yml`.
6. Verify <https://pypi.org/project/mergetrain/> and install from PyPI in a
   fresh environment.

## 0.1.0 highlights

- Local SQLite queue and one lease-fenced runner for coding-agent worktrees.
- Exact validated-train identity with approval-gated, atomic deploys.
- Configurable gates, post-push verification, cancellation, and crash recovery.
- JSON-first agent contract, doctor, status, and garbage collection.
- Loopback-only, read-only live dashboard with runner and gate explanations.
