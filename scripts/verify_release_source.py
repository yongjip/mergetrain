#!/usr/bin/env python3
"""Authenticate one release tag from a protected-main workflow checkout."""

from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_RELEASE_TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")


class ReleaseSourceError(RuntimeError):
    """The requested release source does not satisfy the trust policy."""


@dataclass(frozen=True)
class VerifiedReleaseSource:
    tag: str
    tag_object_sha: str
    commit_sha: str


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "git command failed"
        raise ReleaseSourceError(detail)
    return process


def _validated_tag(tag: str) -> str:
    if not _RELEASE_TAG.fullmatch(tag):
        raise ReleaseSourceError("release tag must match vMAJOR.MINOR.PATCH")
    return tag


def _candidate_ref(tag: str) -> str:
    return f"refs/mergetrain-release-candidates/{tag}"


def verify_release_source(
    repo: Path,
    *,
    tag: str,
    allowed_signers: Path,
    remote: str = "origin",
) -> VerifiedReleaseSource:
    """Verify a signed annotated tag and bind it to current remote main."""

    tag = _validated_tag(tag)
    candidate_ref = _candidate_ref(tag)
    _git(
        repo,
        "fetch",
        "--force",
        "--no-tags",
        remote,
        f"refs/tags/{tag}:{candidate_ref}",
        "refs/heads/main:refs/remotes/origin/main",
    )

    object_type = _git(repo, "cat-file", "-t", candidate_ref).stdout.strip()
    if object_type != "tag":
        raise ReleaseSourceError("release ref must be an annotated tag object")

    signer_file = allowed_signers.resolve()
    if not signer_file.is_file():
        raise ReleaseSourceError(f"allowed signers file does not exist: {signer_file}")
    _git(
        repo,
        "-c",
        f"gpg.ssh.allowedSignersFile={signer_file}",
        "verify-tag",
        candidate_ref,
    )

    tag_object_sha = _git(repo, "rev-parse", candidate_ref).stdout.strip()
    commit_sha = _git(repo, "rev-parse", f"{candidate_ref}^{{commit}}").stdout.strip()
    ancestor = _git(
        repo,
        "merge-base",
        "--is-ancestor",
        commit_sha,
        "refs/remotes/origin/main",
        check=False,
    )
    if ancestor.returncode != 0:
        raise ReleaseSourceError("release tag commit is not contained in current origin/main")

    return VerifiedReleaseSource(
        tag=tag,
        tag_object_sha=tag_object_sha,
        commit_sha=commit_sha,
    )


def recheck_remote_release_source(
    repo: Path,
    *,
    tag: str,
    expected_tag_object_sha: str,
    expected_commit_sha: str,
    remote: str = "origin",
) -> None:
    """Fail if the tag or main ancestry changed after initial verification."""

    tag = _validated_tag(tag)
    if not re.fullmatch(r"[0-9a-f]{40}", expected_tag_object_sha):
        raise ReleaseSourceError("expected tag object must be a full SHA-1")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_commit_sha):
        raise ReleaseSourceError("expected commit must be a full SHA-1")

    remote_tag = _git(repo, "ls-remote", "--refs", remote, f"refs/tags/{tag}")
    rows = [line.split() for line in remote_tag.stdout.splitlines() if line.strip()]
    if rows != [[expected_tag_object_sha, f"refs/tags/{tag}"]]:
        raise ReleaseSourceError("remote release tag changed after verification")

    _git(
        repo,
        "fetch",
        "--force",
        "--no-tags",
        remote,
        "refs/heads/main:refs/remotes/origin/main",
    )
    ancestor = _git(
        repo,
        "merge-base",
        "--is-ancestor",
        expected_commit_sha,
        "refs/remotes/origin/main",
        check=False,
    )
    if ancestor.returncode != 0:
        raise ReleaseSourceError("verified release commit is no longer contained in origin/main")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    subparsers = parser.add_subparsers(dest="operation", required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--tag", required=True)
    verify.add_argument("--allowed-signers", type=Path, required=True)
    verify.add_argument("--github-output", type=Path)

    recheck = subparsers.add_parser("recheck")
    recheck.add_argument("--tag", required=True)
    recheck.add_argument("--expected-tag-object", required=True)
    recheck.add_argument("--expected-commit", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.operation == "verify":
            result = verify_release_source(
                args.repo,
                tag=args.tag,
                allowed_signers=args.allowed_signers,
            )
            output = (
                f"tag={result.tag}\n"
                f"tag_object_sha={result.tag_object_sha}\n"
                f"commit_sha={result.commit_sha}\n"
            )
            if args.github_output is None:
                print(output, end="")
            else:
                with args.github_output.open("a", encoding="utf-8") as stream:
                    stream.write(output)
        else:
            recheck_remote_release_source(
                args.repo,
                tag=args.tag,
                expected_tag_object_sha=args.expected_tag_object,
                expected_commit_sha=args.expected_commit,
            )
    except ReleaseSourceError as exc:
        print(f"release source verification failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
