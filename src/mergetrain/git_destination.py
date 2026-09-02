"""Resolve and pin the one Git endpoint an approved deploy may update."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlsplit, urlunsplit

from .config import MergetrainConfig
from .errors import MergetrainError, redact_secrets
from .git_ops import DEPLOY_AUDIT_REF_PREFIX, git_remote_push_urls, git_remote_url

_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_REMOTE_HELPER = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*::")
_SCP_LIKE = re.compile(r"^(?:[^/@:\\s]+@)?[^/\\:\\s]+:.+$")


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_relative_filesystem_url(url: str) -> bool:
    """Return whether Git would resolve ``url`` relative to its current cwd."""

    if _URI_SCHEME.match(url) or _REMOTE_HELPER.match(url):
        return False
    if Path(url).is_absolute() or PureWindowsPath(url).is_absolute():
        return False
    if re.match(r"^[A-Za-z]:", url):
        return True  # Windows drive-relative path, for example C:repo.git.
    if _SCP_LIKE.match(url):
        return False
    return True


def _credential_free_url(url: str, *, repo: Path) -> str:
    """Return a stable endpoint identity without password/token userinfo."""

    if _URI_SCHEME.match(url):
        parsed = urlsplit(url)
        netloc = parsed.netloc
        if "@" in netloc:
            userinfo, host = netloc.rsplit("@", 1)
            # A non-HTTP username can affect endpoint selection. HTTP(S)
            # userinfo is commonly a token and is authentication, not
            # destination identity.
            if parsed.scheme.lower() not in {"http", "https"}:
                username = userinfo.split(":", 1)[0]
                netloc = f"{username}@{host}" if username else host
            else:
                netloc = host
        return urlunsplit(
            (parsed.scheme.lower(), netloc, parsed.path, parsed.query, parsed.fragment)
        )
    if Path(url).is_absolute():
        return str(Path(url).resolve())
    if PureWindowsPath(url).is_absolute():
        return str(PureWindowsPath(url))
    if _is_relative_filesystem_url(url):
        return str((repo / url).resolve())
    return url


def _canonical_push_url(url: str) -> str:
    """Return the immutable URL spelling that Git commands will actually use.

    Absolute local paths (including local ``file://`` URLs) are resolved now,
    from the control checkout, so a later worktree cwd or symlink change cannot
    redirect the approved endpoint.
    """

    if Path(url).is_absolute():
        return str(Path(url).resolve())
    if _URI_SCHEME.match(url):
        parsed = urlsplit(url)
        if (
            parsed.scheme.lower() == "file"
            and parsed.netloc.lower() in {"", "localhost"}
            and not parsed.query
            and not parsed.fragment
        ):
            local_text = unquote(parsed.path)
            if (  # pragma: no cover - Windows file-URI drive form
                os.name == "nt" and re.match(r"^/[A-Za-z]:", local_text)
            ):
                local_text = local_text[1:]
            local_path = Path(local_text)
            if not local_path.is_absolute():  # pragma: no cover - defensive parser guard
                raise MergetrainError(
                    "relative filesystem Git push URLs are not supported for deploy"
                )
            return local_path.resolve().as_uri()
    return url


@dataclass(frozen=True, slots=True)
class ResolvedGitDestination:
    """One immutable, credential-safe deploy identity and its in-memory URL."""

    remote_name: str
    fetch_url: str
    push_url: str
    display_url: str
    destination_sha: str
    push_endpoint_sha: str
    push_refs: tuple[str, ...]
    remote_alias: str

    def command_env(self) -> dict[str, str]:
        """Pin this endpoint without putting a credential URL in argv or logs.

        A fresh unguessable sentinel is the only rewrite input. Git rewrites it
        once to the frozen endpoint, so rules matching the endpoint itself are
        never applied a second time. A new sentinel for every command also
        prevents a rule inserted between audit lookup and push from tying the
        pin. Values live only in the child environment; the raw endpoint is
        never written to SQLite or a command log.
        """

        env = dict(os.environ)
        raw_count = env.get("GIT_CONFIG_COUNT", "0") or "0"
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise MergetrainError(
                "GIT_CONFIG_COUNT must be an integer before a deploy can pin its destination"
            ) from exc
        if count < 0:
            raise MergetrainError(
                "GIT_CONFIG_COUNT must not be negative before a deploy can pin its destination"
            )
        sentinel = f"mergetrain-pin-{uuid.uuid4().hex}://endpoint"
        pairs = (
            (f"remote.{self.remote_alias}.url", sentinel),
            (f"remote.{self.remote_alias}.pushurl", sentinel),
            (f"url.{self.push_url}.insteadOf", sentinel),
            (f"url.{self.push_url}.pushInsteadOf", sentinel),
        )
        for key, value in pairs:
            env[f"GIT_CONFIG_KEY_{count}"] = key
            env[f"GIT_CONFIG_VALUE_{count}"] = value
            count += 1
        env["GIT_CONFIG_COUNT"] = str(count)
        return env


def resolve_git_destination(config: MergetrainConfig) -> ResolvedGitDestination:
    """Resolve the single effective push URL from the stable control checkout."""

    fetch_url = git_remote_url(config.repo, config.git.remote)
    push_urls = git_remote_push_urls(config.repo, config.git.remote)
    if not fetch_url or not push_urls:
        raise MergetrainError(
            f"could not resolve Git remote {config.git.remote!r} for deploy"
        )
    if len(push_urls) != 1:
        raise MergetrainError(
            "exactly one effective Git push URL is required for an atomic deploy; "
            f"remote {config.git.remote!r} resolves to {len(push_urls)}"
        )
    push_url = push_urls[0]
    if any(character in push_url for character in "\r\n\0"):
        raise MergetrainError("Git push URLs containing control characters are not supported")
    if _is_relative_filesystem_url(push_url):
        raise MergetrainError(
            "relative filesystem Git push URLs are not supported for deploy; "
            f"replace {redact_secrets(push_url)!r} with an absolute path or file:// URL"
        )
    push_url = _canonical_push_url(push_url)
    push_endpoint = _credential_free_url(push_url, repo=config.repo)
    push_endpoint_sha = _sha256_json(
        {
            "version": 1,
            "push_endpoint": push_endpoint,
            "push_refs": list(config.git.push_refs),
            "audit_ref_prefix": DEPLOY_AUDIT_REF_PREFIX,
        }
    )
    identity = {
        "version": 2,
        "remote": config.git.remote,
        "fetch_endpoint": _credential_free_url(fetch_url, repo=config.repo),
        "push_endpoint": push_endpoint,
        "integration_ref": config.git.integration_ref,
        "push_refs": list(config.git.push_refs),
        "audit_ref_prefix": DEPLOY_AUDIT_REF_PREFIX,
    }
    destination_sha = _sha256_json(identity)
    return ResolvedGitDestination(
        remote_name=config.git.remote,
        fetch_url=fetch_url,
        push_url=push_url,
        display_url=redact_secrets(push_url),
        destination_sha=destination_sha,
        push_endpoint_sha=push_endpoint_sha,
        push_refs=tuple(config.git.push_refs),
        remote_alias=f"mergetrain-destination-{uuid.uuid4().hex}",
    )
