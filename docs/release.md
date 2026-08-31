# Release checklist

PyPI releases are built by GitHub Actions and published with short-lived OIDC
credentials. Do not upload production artifacts from a developer machine.

## What CI verifies

Every pull request runs:

- unit tests on macOS and Linux with Python 3.10 through 3.14, plus one blocking
  Windows Python version;
- the installed-CLI E2E suite on macOS and Linux;
- dashboard unit/build checks and a headless Chromium interaction suite for
  notification permission, duplicate tabs, drill-down clicks, and feed recovery;
- version, changelog, and security support-policy consistency checks;
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

Browser automation replaces the OS notification API with a deterministic fake,
so each release that changes alerts also gets one native-browser smoke pass:

- macOS Safari or Chrome and Windows Edge can grant permission from the
  loopback dashboard and show the immediate confirmation alert;
- a disposable Hub repo moving from running to validated produces one alert
  across two open tabs, and clicking it opens that repo's drill-down;
- a live snapshot failure shows `DEGRADED` with the last good state, then clears
  after recovery; and
- closing every dashboard tab stops interactive alerts. Headless webhook
  delivery remains a separate `--notify` check.

## One-time Trusted Publishing setup

Create two GitHub Environments in repository settings:

| Environment | Purpose | Protection |
| --- | --- | --- |
| `testpypi` | Manual TestPyPI rehearsal | No required reviewer (deliberate, 2026-07-21) |
| `pypi` | Production PyPI release | No required reviewer (deliberate, 2026-07-21) |

Both environments intentionally carry **no manual approval gate**: publishing
the GitHub Release is itself the deliberate human act that authorizes the
upload, and adding a second click on top of it only slowed releases down.
OIDC still scopes credentials to these exact workflows, and versions are
immutable once published.

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
accounts with 2FA. **Publishing the GitHub Release is the final human release
boundary** — once it is published, `release.yml` builds and uploads without
further prompts.

## Rehearse on TestPyPI

After the release-preparation pull request is merged:

1. Open **Actions → TestPyPI → Run workflow** on `main`. Triggering the run
   is the deliberate act; it publishes without further approval.
2. Wait for the publish job to complete.
3. Install the exact version from TestPyPI in a fresh environment:

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

1. Confirm all `main` CI checks passed (the TestPyPI rehearsal is optional —
   PR CI already builds both distributions, runs `twine check --strict`, and
   smoke-installs the wheel in a clean environment).
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

5. Publishing the GitHub Release triggers `.github/workflows/release.yml`,
   which builds and uploads to PyPI, then publishes `server.json` to the
   official MCP Registry with GitHub OIDC. There is no further prompt — the
   Release publication in step 4 **is** the approval.
6. Verify <https://pypi.org/project/mergetrain/>, install from PyPI in a fresh
   environment, and confirm the Registry API returns the released version:

   ```sh
   curl \
     "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.yongjip%2Fmergetrain"
   ```

7. The Homebrew tap picks the release up on its own daily cron. To make that
   immediate, see the optional dispatch below; otherwise check
   `brew install yongjip/tap/mergetrain` the next day.

## Publish or repair MCP Registry metadata

`.github/workflows/mcp-registry.yml` is also manually dispatchable from
**Actions → Publish MCP Registry → Run workflow**. Use that path to bootstrap
the first Registry entry or repair a release whose Registry job failed after
PyPI succeeded.

The workflow:

- verifies that `pyproject.toml`, `server.json`, and the changelog describe one
  release;
- downloads a pinned `mcp-publisher` binary and verifies its SHA-256;
- validates `server.json` against the public Registry;
- authenticates with short-lived GitHub Actions OIDC; and
- publishes the manifest.

It stores no Registry or GitHub PAT. The manual workflow must run from `main`,
where `server.json` describes the already-public PyPI version. Registry versions
are immutable, so do not rerun a successful publication without first releasing
a new package version.

## Optional: bump the Homebrew tap on release

[yongjip/homebrew-tap](https://github.com/yongjip/homebrew-tap) rewrites its own
formula from PyPI on a daily schedule, deliberately using no cross-repo
credentials. GitHub disables a scheduled workflow after 60 days without
repository activity, though, which is exactly what a quiet tap looks like — so a
release can leave the formula stale twice over: the cron has not fired yet, and
it may not be armed at all.

The `bump-tap` job in `release.yml` closes both gaps by requesting the tap's
`workflow_dispatch` after a successful publish. It is **skipped unless both** of
these exist, so the default path stays credential-free:

| Setting | Kind | Value |
| --- | --- | --- |
| `HOMEBREW_TAP_REPOSITORY` | repository **variable** | `yongjip/homebrew-tap` |
| `HOMEBREW_TAP_DISPATCH_TOKEN` | repository **secret** | fine-grained PAT, that tap only, `Actions: read and write` |

Scope the token to the tap repository alone and nothing else; it needs no access
to this repository. If it is missing, the job logs that it is leaving the bump to
the cron and succeeds, so a release never fails over tap plumbing.

## 0.1.0 highlights

- Local SQLite queue and one lease-fenced runner for coding-agent worktrees.
- Exact validated-train identity with approval-gated, atomic deploys.
- Configurable gates, post-push verification, cancellation, and crash recovery.
- JSON-first agent contract, doctor, status, and garbage collection.
- Loopback-only, read-only live dashboard with runner and gate explanations.
