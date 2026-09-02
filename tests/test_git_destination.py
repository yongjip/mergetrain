from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mergetrain.atomic_push import AtomicPush
from mergetrain.config import load_config
from mergetrain.deploy_plan import deploy_destination_sha
from mergetrain.errors import MergetrainError
from mergetrain.git_destination import (
    _credential_free_url,
    _is_relative_filesystem_url,
    resolve_git_destination,
)


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=False
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed\n{completed.stdout}\n{completed.stderr}"
        )
    return completed.stdout.strip()


def make_repo(root: Path) -> tuple[Path, Path, Path, Path]:
    remote_a = root / "remote-a.git"
    remote_b = root / "remote-b.git"
    remote_c = root / "remote-c.git"
    repo = root / "repo"
    for remote in (remote_a, remote_b, remote_c):
        git(root, "init", "--bare", str(remote))
    git(root, "clone", str(remote_a), str(repo))
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test User")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "base.txt")
    git(repo, "commit", "-m", "base")
    git(repo, "branch", "-M", "main")
    git(repo, "push", "-u", "origin", "main")
    git(repo, "push", str(remote_b), "main:main")
    (repo / ".mergetrain.yaml").write_text(
        f"""project:
  name: destination-test
state:
  db: {root / 'queue.sqlite'}
  logs: {root / 'logs'}
  worktree_root: {root / 'worktrees'}
git:
  remote: origin
  integration_branch: main
  push_refs:
    - main
deploy:
  verify: []
""",
        encoding="utf-8",
    )
    return repo, remote_a, remote_b, remote_c


class GitDestinationTests(unittest.TestCase):
    def test_supported_url_forms_are_classified_without_cwd_ambiguity(self) -> None:
        self.assertTrue(_is_relative_filesystem_url("C:repo.git"))
        self.assertFalse(_is_relative_filesystem_url("git@example.invalid:repo.git"))
        self.assertFalse(_is_relative_filesystem_url("ext::helper command"))
        self.assertFalse(_is_relative_filesystem_url("file:///tmp/repo.git"))
        self.assertFalse(_is_relative_filesystem_url(r"C:\\repos\\repo.git"))
        self.assertEqual(
            _credential_free_url("ext::helper command", repo=Path("/unused")),
            "ext::helper command",
        )
        self.assertEqual(
            _credential_free_url(r"C:\\repos\\repo.git", repo=Path("/unused")),
            r"C:\repos\repo.git",
        )

    def test_command_environment_rejects_invalid_inherited_config_count(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _remote_a, remote_b, _remote_c = make_repo(Path(td))
            git(repo, "remote", "set-url", "--push", "origin", str(remote_b))
            destination = resolve_git_destination(load_config(repo=repo))

            for value, message in (("not-an-int", "must be an integer"), ("-1", "negative")):
                with self.subTest(value=value), patch.dict(
                    os.environ, {"GIT_CONFIG_COUNT": value}
                ):
                    with self.assertRaisesRegex(MergetrainError, message):
                        destination.command_env()

    def test_command_environment_uses_a_fresh_private_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _remote_a, remote_b, _remote_c = make_repo(Path(td))
            git(repo, "remote", "set-url", "--push", "origin", str(remote_b))
            destination = resolve_git_destination(load_config(repo=repo))

            first = destination.command_env()
            second = destination.command_env()
            alias_key = next(
                key
                for key, value in first.items()
                if key.startswith("GIT_CONFIG_KEY_")
                and value == f"remote.{destination.remote_alias}.pushurl"
            )
            index = alias_key.rsplit("_", 1)[1]
            first_sentinel = first[f"GIT_CONFIG_VALUE_{index}"]
            second_sentinel = second[f"GIT_CONFIG_VALUE_{index}"]

            self.assertNotEqual(first_sentinel, second_sentinel)
            self.assertTrue(first_sentinel.startswith("mergetrain-pin-"))
            self.assertNotIn(str(remote_b), first_sentinel)

    def test_http_credentials_rotate_without_changing_endpoint_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _remote_a, _remote_b, _remote_c = make_repo(Path(td))
            first_url = "https://runner:first-secret@example.invalid/repo.git"
            second_url = "https://runner:second-secret@example.invalid/repo.git"
            git(repo, "remote", "set-url", "--push", "origin", first_url)
            first = resolve_git_destination(load_config(repo=repo))
            git(repo, "remote", "set-url", "--push", "origin", second_url)
            second = resolve_git_destination(load_config(repo=repo))

            self.assertEqual(first.destination_sha, second.destination_sha)
            self.assertEqual(first.push_endpoint_sha, second.push_endpoint_sha)
            self.assertNotIn("first-secret", first.display_url)
            self.assertIn("[redacted]", first.display_url)

    def test_relative_fetch_url_is_canonicalized_from_the_control_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, remote_a, remote_b, _remote_c = make_repo(Path(td))
            git(repo, "remote", "set-url", "origin", "../remote-a.git")
            git(repo, "remote", "set-url", "--push", "origin", str(remote_b))
            relative = resolve_git_destination(load_config(repo=repo))
            git(repo, "remote", "set-url", "origin", str(remote_a))
            absolute = resolve_git_destination(load_config(repo=repo))

            self.assertEqual(relative.destination_sha, absolute.destination_sha)

    def test_control_characters_in_push_url_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _remote_a, _remote_b, _remote_c = make_repo(Path(td))
            with patch(
                "mergetrain.git_destination.git_remote_push_urls",
                return_value=("https://example.invalid/repo.git\nother",),
            ):
                with self.assertRaisesRegex(MergetrainError, "control characters"):
                    resolve_git_destination(load_config(repo=repo))

    def test_pushurl_is_the_approved_and_actual_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, remote_a, remote_b, _remote_c = make_repo(Path(td))
            base = git(remote_a, "rev-parse", "main")
            git(repo, "remote", "set-url", "--push", "origin", str(remote_b))
            (repo / "next.txt").write_text("next\n", encoding="utf-8")
            git(repo, "add", "next.txt")
            git(repo, "commit", "-m", "next")
            head = git(repo, "rev-parse", "HEAD")
            config = load_config(repo=repo)
            destination = resolve_git_destination(config)

            self.assertEqual(destination.fetch_url, str(remote_a))
            self.assertEqual(destination.push_url, str(remote_b.resolve()))
            self.assertEqual(deploy_destination_sha(config), destination.destination_sha)

            log = io.StringIO()
            AtomicPush(config).push_verified_head(
                worktree=repo,
                deploy_sha=head,
                log=log,
                destination=destination,
            )

            self.assertEqual(git(remote_a, "rev-parse", "main"), base)
            self.assertEqual(git(remote_b, "rev-parse", "main"), head)
            command_lines = [
                line for line in log.getvalue().splitlines() if line.startswith("$")
            ]
            self.assertNotIn(str(remote_b), "\n".join(command_lines))
            self.assertIn(destination.remote_alias, log.getvalue())

    def test_resolved_endpoint_is_pinned_against_later_exact_url_rewrites(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _remote_a, remote_b, remote_c = make_repo(Path(td))
            git(repo, "remote", "set-url", "--push", "origin", str(remote_b))
            config = load_config(repo=repo)
            destination = resolve_git_destination(config)
            (repo / "pinned.txt").write_text("pinned\n", encoding="utf-8")
            git(repo, "add", "pinned.txt")
            git(repo, "commit", "-m", "pinned")
            head = git(repo, "rev-parse", "HEAD")
            push = AtomicPush(config)

            def mutate_after_audit(**kwargs):  # type: ignore[no-untyped-def]
                result = push.audit_ref_expectation(**kwargs)
                # These exact-length rules beat the old command-scope
                # self-rule on Git 2.55. Insert them after the audit lookup to
                # exercise the precise audit-to-push race from review.
                git(
                    repo,
                    "config",
                    "--add",
                    f"url.{remote_c}.insteadOf",
                    str(remote_b.resolve()),
                )
                git(
                    repo,
                    "config",
                    "--add",
                    f"url.{remote_c}.pushInsteadOf",
                    str(remote_b.resolve()),
                )
                return result

            push.push_verified_head(
                worktree=repo,
                deploy_sha=head,
                destination=destination,
                audit_expectation=mutate_after_audit,
            )

            self.assertEqual(git(remote_b, "rev-parse", "main"), head)
            self.assertEqual(
                subprocess.run(
                    ["git", "show-ref"],
                    cwd=remote_c,
                    text=True,
                    capture_output=True,
                    check=False,
                ).stdout,
                "",
            )

    def test_absolute_local_symlink_is_frozen_to_its_resolved_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _remote_a, remote_b, remote_c = make_repo(root)
            remote_link = root / "approved-remote.git"
            try:
                remote_link.symlink_to(remote_b, target_is_directory=True)
            except OSError as exc:  # pragma: no cover - platform capability
                self.skipTest(f"directory symlinks unavailable: {exc}")
            git(repo, "remote", "set-url", "--push", "origin", str(remote_link))
            config = load_config(repo=repo)
            destination = resolve_git_destination(config)
            self.assertEqual(destination.push_url, str(remote_b.resolve()))

            remote_link.unlink()
            remote_link.symlink_to(remote_c, target_is_directory=True)
            (repo / "symlink-pinned.txt").write_text("pinned\n", encoding="utf-8")
            git(repo, "add", "symlink-pinned.txt")
            git(repo, "commit", "-m", "symlink pinned")
            head = git(repo, "rev-parse", "HEAD")

            AtomicPush(config).push_verified_head(
                worktree=repo,
                deploy_sha=head,
                destination=destination,
            )

            self.assertEqual(git(remote_b, "rev-parse", "main"), head)
            self.assertEqual(
                subprocess.run(
                    ["git", "show-ref"],
                    cwd=remote_c,
                    text=True,
                    capture_output=True,
                    check=False,
                ).stdout,
                "",
            )

    def test_local_file_url_is_canonicalized(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _remote_a, remote_b, _remote_c = make_repo(root)
            remote_link = root / "file-url-link.git"
            try:
                remote_link.symlink_to(remote_b, target_is_directory=True)
            except OSError as exc:  # pragma: no cover - platform capability
                self.skipTest(f"directory symlinks unavailable: {exc}")
            git(
                repo,
                "remote",
                "set-url",
                "--push",
                "origin",
                remote_link.as_uri(),
            )

            destination = resolve_git_destination(load_config(repo=repo))

            self.assertEqual(destination.push_url, remote_b.resolve().as_uri())

    def test_push_instead_of_is_bound_before_approval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _remote_a, remote_b, _remote_c = make_repo(Path(td))
            source = "https://example.invalid/mergetrain.git"
            git(repo, "remote", "set-url", "origin", source)
            git(repo, "config", f"url.{remote_b}.pushInsteadOf", source)

            destination = resolve_git_destination(load_config(repo=repo))

            self.assertEqual(destination.fetch_url, source)
            self.assertEqual(destination.push_url, str(remote_b.resolve()))

    def test_multiple_push_urls_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _remote_a, remote_b, remote_c = make_repo(Path(td))
            git(repo, "config", "--add", "remote.origin.pushurl", str(remote_b))
            git(repo, "config", "--add", "remote.origin.pushurl", str(remote_c))

            with self.assertRaisesRegex(
                MergetrainError, "exactly one effective Git push URL"
            ):
                resolve_git_destination(load_config(repo=repo))

    def test_relative_filesystem_push_url_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _remote_a, _remote_b, _remote_c = make_repo(Path(td))
            git(repo, "remote", "set-url", "--push", "origin", "../remote-b.git")

            with self.assertRaisesRegex(
                MergetrainError, "relative filesystem Git push URLs"
            ):
                resolve_git_destination(load_config(repo=repo))

    def test_ssh_usernames_remain_part_of_destination_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _remote_a, _remote_b, _remote_c = make_repo(Path(td))
            git(repo, "remote", "set-url", "origin", "ssh://alice@example.invalid/repo")
            alice = deploy_destination_sha(load_config(repo=repo))
            git(repo, "remote", "set-url", "origin", "ssh://bob@example.invalid/repo")
            bob = deploy_destination_sha(load_config(repo=repo))

            self.assertNotEqual(alice, bob)


if __name__ == "__main__":
    unittest.main()
