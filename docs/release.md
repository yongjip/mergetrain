# Release checklist

PyPI releases are built by GitHub Actions and published with short-lived OIDC
credentials. Do not upload production artifacts from a developer machine.

## When to cut a release

Merging to `main` is not a reason to release. Every published version is
immutable, gets fetched by PyPI mirrors and scanners, and asks every user to
upgrade. Between 0.1.0 (2026-07-16) and 3.2.0 (2026-09-29) mergetrain shipped
36 releases, including 2.0.0 and 3.0.0 four days apart. That pace inflates
download counts without adding users and makes the tool look unstable to the
people who depend on it.

- Feature and fix changes add their `CHANGELOG.md` entry under
  `## Unreleased` and leave every version string alone. Only the
  release-preparation change bumps the version, once for everything pending:
  it turns `## Unreleased` into the dated heading and updates every version
  surface that `scripts/check_release.py` checks, including the version that
  `docs/contract.md` and the Claude plugin README quote, and the pin in
  `tests/test_mcp_registry_launch.py`. `.gitattributes` merges
  `CHANGELOG.md` with Git's union driver, so entries added by parallel
  branches do not conflict.
- Ship a patch release promptly only for a regression in the latest release, a
  data-loss or safety-boundary bug (an unapproved push, or a cleanup that
  deletes files mergetrain does not own), a security fix, or a broken install
  or launch path (PyPI, `uvx`, the MCP Registry, or the plugins). Fold every
  other fix into the next release.
- Ship at most one feature (minor) release per week.
- Bump the major version only for a breaking change to the machine contract,
  the CLI grammar, or the configuration schema. Group breaking changes into one
  planned major release instead of shipping several majors in a row. The one
  exception is removing machine output whose only consumer inside mergetrain is
  gone: once the owner decides it and docs/contract.md records the contract
  bump and migration, it may ship in a minor release, as contract 5 did in
  3.4.0.
- Do not publish a second version on the same day unless it fixes a regression
  in the first.
- Changes to docs, tests, CI, or internal structure alone never justify a
  release.

An agent asked to prepare a release first lists what is pending under
`## Unreleased` and says whether it meets these rules. If it does not, the
agent recommends waiting instead of preparing the release. An agent whose
change meets the patch-release criteria above says so when it hands the change
off, and recommends a prompt patch release instead of waiting to be asked.

## What CI verifies

Every pull request runs:

- unit tests on macOS and Linux with Python 3.10 through 3.14, plus one blocking
  Windows Python version;
- version, changelog, and security support-policy consistency checks;
- immutable full-commit pins for every external GitHub Action;
- isolated sdist and wheel builds, followed by extraction and execution of the
  packaged sdist's own collection and test suite;
- `twine check --strict` on both distributions; and
- a clean-environment wheel install that confirms Python imports the installed
  package and runs the self-checking `mergetrain demo --brief` walkthrough; and
- a clean MCP-extra wheel install that starts the stdio server, initializes the
  protocol, lists tools, and verifies the deploy input schema.

The same metadata, unit, build, and strict package checks run again from the
verified release commit before any job receives PyPI credentials. A
`workflow_dispatch` run rooted at protected `main` authenticates the annotated
tag with main's `.github/release-allowed-signers`, requires the tag commit in
current `origin/main`, and builds the captured commit SHA rather than resolving
the tag again. The build artifacts receive GitHub attestations before
publication.

Useful local equivalents:

```sh
python -m pip install -e ".[dev]"
python -m pytest -q
python scripts/check_release.py --tag v0.1.0
python -m build
python -m twine check --strict dist/*
smoke="$(mktemp -d)/venv"
python -m venv "$smoke"
"$smoke/bin/python" -m pip install dist/mergetrain-0.1.0-py3-none-any.whl
"$smoke/bin/mergetrain" --version
"$smoke/bin/mergetrain" demo --brief
bash scripts/check_sdist.sh dist/mergetrain-0.1.0.tar.gz
```

## One-time Trusted Publishing setup

Create two GitHub Environments in repository settings:

| Environment | Purpose | Protection |
| --- | --- | --- |
| `testpypi` | Manual TestPyPI rehearsal | Selected branch `main`; optional reviewer |
| `pypi` | Production PyPI release | Selected branch `main`; no second click after Release publication |

The deployment branch rule is part of the trust boundary, not a convenience:
both environments must reject tag and non-main workflow runs. Publishing the
immutable GitHub Release is the deliberate production act that authorizes the
upload; the later main-rooted workflow dispatch is its mechanical continuation,
not another per-artifact approval. OIDC scopes credentials to the exact workflow
and environment, and package versions are immutable once published.

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
boundary.** Production publication still requires an explicit dispatch of
`release.yml` at `main`; it refuses another branch or tag ref and refuses a
release that GitHub has not marked immutable.

## One-time release signing setup

Release tags use an Ed25519 SSH signing key that is separate from repository
authentication. Register its public key in GitHub under **Settings → SSH and
GPG keys → New SSH signing key**; do not add it as an authentication key. Keep
the private key outside the repository with mode `0600`, then configure this
checkout:

```sh
git config --local gpg.format ssh
git config --local user.signingkey ~/.ssh/mergetrain-release-signing.pub
git config --local gpg.ssh.allowedSignersFile \
  "$PWD/.github/release-allowed-signers"
git config --local tag.gpgSign true
```

The tracked allowed-signers file contains public material only and enables
local and CI verification. To rotate a key, land the new public key through the
normal reviewed integration path before using it; the publisher reads that
policy from main, never from the release tag. Retain an old key while tags
signed by it still need verification. Never rewrite an existing release tag.

## Rehearse on TestPyPI

After the release-preparation change is integrated and its signed tag is pushed:

1. Open **Actions → TestPyPI → Run workflow** on `main`, enter the signed tag,
   and trigger the run. The workflow authenticates the tag with main's signer
   policy and builds its captured commit SHA.
2. Wait for the publish job to complete.
3. Install the exact version from TestPyPI in a fresh environment:

   ```sh
   python -m venv /tmp/mergetrain-testpypi
   /tmp/mergetrain-testpypi/bin/python -m pip install \
     --index-url https://test.pypi.org/simple/ --no-deps mergetrain==0.1.0
   /tmp/mergetrain-testpypi/bin/mergetrain --version
   /tmp/mergetrain-testpypi/bin/mergetrain status --help
   ```

Package versions are immutable on each index. Bump the version before repeating
an upload that already succeeded.

## Publish to production

1. Confirm all `main` CI checks passed (the TestPyPI rehearsal is optional —
   PR CI already builds both distributions, runs `twine check --strict`, and
   smoke-installs the wheel in a clean environment).
2. Confirm catalog descriptions match the canonical discovery metadata:

   ```sh
   python scripts/check_discovery_metadata.py
   python scripts/check_discovery_metadata.py --github-json
   agy plugin validate .
   ```

   Compare the second command's description and topic set with GitHub About and
   update that repository setting if it drifted. Do not maintain a second copy
   of the desired text in this checklist.
3. Update the version and turn `## Unreleased` into the dated changelog heading
   for the intended release. Read the section first: the union merge that
   `.gitattributes` sets for `CHANGELOG.md` can splice together two parallel
   entries that share an identical line. If the release changes anything that
   scripts or agents built on mergetrain can notice, such as the machine
   contract, an exit code, a configuration check, or a command's behavior,
   open the section with an Upgrade notes list ahead of its Changes, as 3.4.0
   does.
4. Create a signed annotated tag on the exact verified `main` commit, verify it
   locally against the tracked allowed signer, and push it. Unsigned release
   tags are rejected by the release workflow:

   ```sh
   git switch main
   git pull --ff-only
   python scripts/check_release.py --tag v0.1.0
   git tag -s v0.1.0 -m "mergetrain 0.1.0"
   git verify-tag v0.1.0
   git push origin v0.1.0
   ```

5. Confirm repository-level **Release immutability** is enabled. It applies only
   to future Releases, so do this before publishing the release.
6. Publish a GitHub Release for that existing tag. Once published, the tag and
   any attached assets are locked:

   ```sh
   gh release create v0.1.0 --verify-tag --generate-notes \
     --title "mergetrain 0.1.0"
   ```

7. Dispatch the trusted workflow explicitly at `main` with the same tag:

   ```sh
   gh workflow run release.yml --ref main -f tag=v0.1.0
   ```

   The workflow refuses non-main dispatches, mutable/draft Releases, untrusted
   signatures, lightweight tags, and commits outside current main before any
   tag-provided code or package-publishing credential is used. It builds and
   uploads to PyPI, then publishes `server.json` to the official MCP Registry.
   The Release publication in step 6 remains the human approval.
8. Verify <https://pypi.org/project/mergetrain/>, install from PyPI in a fresh
   environment, verify its GitHub artifact attestation, and confirm the Registry
   API returns the released version:

   ```sh
   curl \
     "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.yongjip%2Fmergetrain"
   gh attestation verify dist/mergetrain-0.1.0-py3-none-any.whl \
     --repo yongjip/mergetrain
   ```

9. The Homebrew tap picks the release up on its own daily cron. To make that
   immediate, see the optional dispatch below; otherwise check
   `brew install yongjip/tap/mergetrain` the next day.

## Publish or repair MCP Registry metadata

`.github/workflows/mcp-registry.yml` is also manually dispatchable from
**Actions → Publish MCP Registry → Run workflow** on `main`. Use that path to bootstrap
the first Registry entry or repair a release whose Registry job failed after
PyPI succeeded.

The workflow:

- verifies that `pyproject.toml`, `server.json`, and the changelog describe one
  release;
- downloads a pinned `mcp-publisher` binary and verifies its SHA-256;
- validates `server.json` against the public Registry;
- constructs the manifest's exact `uvx --from mergetrain[mcp]==<version>`
  command against PyPI and verifies an MCP initialize + `tools/list` handshake,
  retrying with an isolated uv cache so stale negative metadata cannot poison a
  just-published release;
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
- JSON-first status, generated agent instructions, inspection, and garbage collection.
- Loopback-only, read-only live dashboard with runner and gate explanations.
