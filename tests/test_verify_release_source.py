from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.verify_release_source import (
    ReleaseSourceError,
    recheck_remote_release_source,
    verify_release_source,
)


def _run(cwd: Path, *args: str) -> str:
    return subprocess.run(
        list(args),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is required")
class VerifyReleaseSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.remote = root / "remote.git"
        self.repo = root / "repo"
        self.key = root / "release-key"
        self.other_key = root / "other-key"
        self.allowed_signers = root / "allowed-signers"

        _run(root, "git", "init", "--bare", str(self.remote))
        _run(root, "git", "init", "-b", "main", str(self.repo))
        _run(self.repo, "git", "config", "user.name", "Release Test")
        _run(self.repo, "git", "config", "user.email", "release@example.com")
        _run(self.repo, "git", "remote", "add", "origin", str(self.remote))
        (self.repo / "tracked.txt").write_text("main\n", encoding="utf-8")
        _run(self.repo, "git", "add", "tracked.txt")
        _run(self.repo, "git", "commit", "-m", "initial")
        _run(self.repo, "git", "push", "-u", "origin", "main")

        _run(root, "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key))
        _run(
            root,
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-f",
            str(self.other_key),
        )
        public_key = self.key.with_suffix(".pub").read_text(encoding="utf-8").strip()
        self.allowed_signers.write_text(
            f"release@example.com {public_key}\n", encoding="utf-8"
        )
        _run(self.repo, "git", "config", "gpg.format", "ssh")
        _run(self.repo, "git", "config", "user.signingkey", str(self.key))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _signed_tag(self, tag: str) -> None:
        _run(self.repo, "git", "tag", "-s", tag, "-m", tag)
        _run(self.repo, "git", "push", "origin", f"refs/tags/{tag}")

    def test_accepts_allowed_signed_tag_on_main_and_rechecks_it(self) -> None:
        self._signed_tag("v2.4.2")

        verified = verify_release_source(
            self.repo,
            tag="v2.4.2",
            allowed_signers=self.allowed_signers,
        )
        recheck_remote_release_source(
            self.repo,
            tag=verified.tag,
            expected_tag_object_sha=verified.tag_object_sha,
            expected_commit_sha=verified.commit_sha,
        )

        self.assertEqual(verified.commit_sha, _run(self.repo, "git", "rev-parse", "HEAD"))

    def test_accepts_tag_after_main_advances(self) -> None:
        self._signed_tag("v2.4.2")
        tagged_commit = _run(self.repo, "git", "rev-parse", "HEAD")
        (self.repo / "later.txt").write_text("later\n", encoding="utf-8")
        _run(self.repo, "git", "add", "later.txt")
        _run(self.repo, "git", "commit", "-m", "later")
        _run(self.repo, "git", "push", "origin", "main")

        verified = verify_release_source(
            self.repo,
            tag="v2.4.2",
            allowed_signers=self.allowed_signers,
        )

        self.assertEqual(verified.commit_sha, tagged_commit)

    def test_rejects_untrusted_signature(self) -> None:
        _run(self.repo, "git", "config", "user.signingkey", str(self.other_key))
        self._signed_tag("v2.4.2")

        with self.assertRaises(ReleaseSourceError):
            verify_release_source(
                self.repo,
                tag="v2.4.2",
                allowed_signers=self.allowed_signers,
            )

    def test_rejects_lightweight_tag(self) -> None:
        _run(self.repo, "git", "tag", "v2.4.2")
        _run(self.repo, "git", "push", "origin", "refs/tags/v2.4.2")

        with self.assertRaisesRegex(ReleaseSourceError, "annotated tag"):
            verify_release_source(
                self.repo,
                tag="v2.4.2",
                allowed_signers=self.allowed_signers,
            )

    def test_rejects_signed_commit_outside_main(self) -> None:
        _run(self.repo, "git", "switch", "-c", "side")
        (self.repo / "side.txt").write_text("side\n", encoding="utf-8")
        _run(self.repo, "git", "add", "side.txt")
        _run(self.repo, "git", "commit", "-m", "side")
        self._signed_tag("v2.4.2")

        with self.assertRaisesRegex(ReleaseSourceError, "not contained"):
            verify_release_source(
                self.repo,
                tag="v2.4.2",
                allowed_signers=self.allowed_signers,
            )

    def test_recheck_rejects_moved_remote_tag(self) -> None:
        self._signed_tag("v2.4.2")
        verified = verify_release_source(
            self.repo,
            tag="v2.4.2",
            allowed_signers=self.allowed_signers,
        )
        (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        _run(self.repo, "git", "add", "tracked.txt")
        _run(self.repo, "git", "commit", "-m", "changed")
        _run(self.repo, "git", "push", "origin", "main")
        _run(self.repo, "git", "tag", "-f", "-s", "v2.4.2", "-m", "moved")
        _run(self.repo, "git", "push", "--force", "origin", "refs/tags/v2.4.2")

        with self.assertRaisesRegex(ReleaseSourceError, "changed after verification"):
            recheck_remote_release_source(
                self.repo,
                tag=verified.tag,
                expected_tag_object_sha=verified.tag_object_sha,
                expected_commit_sha=verified.commit_sha,
            )

    def test_rejects_non_release_tag_before_git_mutation(self) -> None:
        with self.assertRaisesRegex(ReleaseSourceError, "vMAJOR.MINOR.PATCH"):
            verify_release_source(
                self.repo,
                tag="v2.4.2;touch-owned",
                allowed_signers=self.allowed_signers,
            )

        self.assertFalse((self.repo / "touch-owned").exists())
