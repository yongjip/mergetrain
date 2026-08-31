"""Configuration loading for mergetrain.

Project policy is parsed with PyYAML's safe loader and then validated into the
typed runtime configuration below.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from .errors import ConfigError
from .path_gates import validate_gate_path_pattern

DEFAULT_CONFIG_NAME = ".mergetrain.yaml"

# Schema version of `.mergetrain.yaml` (issue #44). A file with no `version:`
# key is treated as version 1 (every file written before versioning existed).
# Mirrors store.SCHEMA_VERSION / registry.REGISTRY_VERSION: one integer per
# artifact, forward-only. Enforcement of a too-new config is command-scoped
# (the deploy/enqueue path fails closed; recovery and read-only commands stay
# permissive) — not done inside load_config, so a version mismatch after a
# rollback can never lock an operator out of crash recovery.
CONFIG_VERSION = 2
NOTIFY_TRANSITIONS = ("landed", "blocked", "needs_reconcile", "daemon_paused")
BUILTIN_DIFF_CHECK_NAME = "diff-check"
BUILTIN_DIFF_CHECK_TEMPLATE = "git diff --check ${integration_ref}..HEAD"


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    name: str


@dataclass(frozen=True, slots=True)
class ValidationWorkspaceConfig:
    mode: str = "ephemeral"
    cache_key: str = ""
    cache_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StateConfig:
    db: Path
    logs: Path
    worktree_root: Path
    validation_workspace: ValidationWorkspaceConfig = ValidationWorkspaceConfig()


@dataclass(frozen=True, slots=True)
class GitConfig:
    remote: str = "origin"
    integration_branch: str = "main"
    push_refs: tuple[str, ...] = ("main",)

    @property
    def integration_ref(self) -> str:
        return f"{self.remote}/{self.integration_branch}"


@dataclass(frozen=True, slots=True)
class QueueConfig:
    lock_ttl_minutes: int = 30
    daemon_interval_seconds: int = 15
    heartbeat_interval_seconds: int = 10
    command_timeout_seconds: int = 3600


@dataclass(frozen=True, slots=True)
class NotifyConfig:
    webhook_url: str = ""
    transitions: tuple[str, ...] = NOTIFY_TRANSITIONS
    timeout_seconds: int = 10

    def to_dict(self) -> dict[str, Any]:
        # A webhook commonly embeds a secret token. Observation surfaces only
        # reveal whether one is configured, never the credential-bearing URL.
        return {
            "webhook_configured": bool(self.webhook_url),
            "transitions": list(self.transitions),
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class GateConfig:
    name: str
    run: str
    always_rerun_on_deploy: bool = False
    paths: tuple[str, ...] = ()
    parallel_group: str = ""
    needs: tuple[str, ...] = ()
    workers: int = 1
    timeout_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class GateParallelismConfig:
    """Resource ceiling for configured pre-push gates.

    ``max_workers=1`` deliberately preserves the historical sequential
    execution model. A gate's ``workers`` value is a resource weight, not a
    request to create that many processes; the gate command remains responsible
    for its own internal worker pool.
    """

    max_workers: int = 1
    timeout_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class ReuseConfig:
    enabled: bool = False
    max_age_minutes: int = 60
    on_mismatch: str = "rerun"
    fingerprints: tuple[GateConfig, ...] = ()


@dataclass(frozen=True, slots=True)
class DeployConfig:
    verify: tuple[GateConfig, ...] = ()
    reuse: ReuseConfig = ReuseConfig()


@dataclass(frozen=True, slots=True)
class MergetrainConfig:
    project: ProjectConfig
    state: StateConfig
    git: GitConfig
    queue: QueueConfig
    gates: tuple[GateConfig, ...]
    gate_parallelism: GateParallelismConfig
    deploy: DeployConfig
    repo: Path
    config_path: Path
    config_exists: bool
    notify: NotifyConfig = NotifyConfig()
    config_version: int = CONFIG_VERSION

    @property
    def validation_worktree_path(self) -> Path:
        return (
            self.state.worktree_root
            / f"{self.project.name}-validation-workspace"
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        workspace = data["state"].pop("validation_workspace")
        data["state"] = {
            key: str(value) for key, value in data["state"].items()
        }
        workspace["cache_paths"] = list(workspace["cache_paths"])
        workspace["path"] = str(self.validation_worktree_path)
        data["state"]["validation_workspace"] = workspace
        data["repo"] = str(self.repo)
        data["config_path"] = str(self.config_path)
        data["git"]["integration_ref"] = self.git.integration_ref
        data["notify"] = self.notify.to_dict()
        for gate in data["gates"]:
            paths = gate.get("paths", ())
            if paths:
                gate["paths"] = list(paths)
            else:
                gate.pop("paths", None)
            needs = gate.get("needs", ())
            if needs:
                gate["needs"] = list(needs)
            else:
                gate.pop("needs", None)
            if not gate.get("parallel_group"):
                gate.pop("parallel_group", None)
            if gate.get("workers") == 1:
                gate.pop("workers", None)
            if gate.get("timeout_seconds") is None:
                gate.pop("timeout_seconds", None)
        for key in ("verify",):
            for gate in data["deploy"][key]:
                for field in (
                    "paths",
                    "parallel_group",
                    "needs",
                    "workers",
                    "timeout_seconds",
                ):
                    gate.pop(field, None)
        for gate in data["deploy"]["reuse"]["fingerprints"]:
            for field in (
                "paths",
                "parallel_group",
                "needs",
                "workers",
                "timeout_seconds",
            ):
                gate.pop(field, None)
        return data


def is_redundant_builtin_diff_check(
    gate: GateConfig,
    *,
    integration_ref: str,
) -> bool:
    """Return whether a configured gate exactly repeats the built-in check.

    Keep the raw configuration visible for diagnostics, but remove this one
    historical default from the effective plan. Customized gates that merely
    reuse the name remain configured work and are never discarded implicitly.
    """

    if (
        gate.name != BUILTIN_DIFF_CHECK_NAME
        or gate.always_rerun_on_deploy
        or gate.paths
        or gate.parallel_group
        or gate.needs
        or gate.workers != 1
        or gate.timeout_seconds is not None
    ):
        return False
    expanded = gate.run.replace("${integration_ref}", integration_ref)
    expected = BUILTIN_DIFF_CHECK_TEMPLATE.replace(
        "${integration_ref}", integration_ref
    )
    try:
        return shlex.split(expanded) == shlex.split(expected)
    except ValueError:
        return False


def effective_gates(config: MergetrainConfig) -> tuple[GateConfig, ...]:
    """Configured gates after removing an exact built-in duplicate."""

    return tuple(
        gate
        for gate in config.gates
        if not is_redundant_builtin_diff_check(
            gate,
            integration_ref=config.git.integration_ref,
        )
    )


_LEGACY_YAML_BOOLEAN = re.compile(r"^(?:yes|no|on|off)$", re.IGNORECASE)
_AMBIGUOUS_YAML_INTEGER = re.compile(
    r"^[+-]?(?:0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+|0[0-9]+)$"
)


def _validate_yaml_text(text: str) -> None:
    """Reject legacy YAML scalars that are unsafe in operator policy.

    PyYAML follows YAML 1.1 scalar rules, where values such as ``no`` and
    ``010`` can become booleans or non-decimal integers. A deploy configuration
    must make those meanings explicit; quoting preserves the string form.
    """

    for raw in text.splitlines():
        leading = raw[: len(raw) - len(raw.lstrip(" \t"))]
        if "\t" in leading:
            raise ConfigError("tab indentation is unsupported; use spaces")
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc

    seen: set[int] = set()

    def validate_node(node: Node) -> None:
        identity = id(node)
        if identity in seen:
            return
        seen.add(identity)
        if isinstance(node, ScalarNode) and node.style is None:
            if _LEGACY_YAML_BOOLEAN.fullmatch(node.value):
                raise ConfigError(
                    "ambiguous YAML boolean; use true/false or quote it as a string"
                )
            if _AMBIGUOUS_YAML_INTEGER.fullmatch(node.value):
                raise ConfigError(
                    "ambiguous YAML integer; use an unprefixed decimal value or quote it"
                )
            return
        if isinstance(node, SequenceNode):
            for child in node.value:
                validate_node(child)
            return
        if isinstance(node, MappingNode):
            # Mapping keys name schema fields and were never scalar-validated;
            # preserve that compatibility while checking every nested value.
            for _key, value in node.value:
                validate_node(value)

    if root is not None:
        validate_node(root)


def load_yaml(text: str) -> dict[str, Any]:
    _validate_yaml_text(text)
    try:
        loaded = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError("top-level YAML value must be a mapping")
    return loaded


def default_config_dict(project_name: str = "example-app") -> dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        "project": {"name": project_name},
        "state": {
            "db": ".mergetrain/queue.sqlite",
            "logs": ".mergetrain/logs",
            "worktree_root": ".mergetrain/worktrees",
            "validation_workspace": {
                "mode": "ephemeral",
                "cache_key": "",
                "cache_paths": [],
            },
        },
        "git": {"remote": "origin", "integration_branch": "main", "push_refs": ["main"]},
        "queue": {
            "lock_ttl_minutes": 30,
            "daemon_interval_seconds": 15,
            "heartbeat_interval_seconds": 10,
            "command_timeout_seconds": 3600,
        },
        "notify": {
            "webhook_url": "",
            "transitions": list(NOTIFY_TRANSITIONS),
            "timeout_seconds": 10,
        },
        "gate_parallelism": {"max_workers": 1},
        "gates": [],
        "deploy": {"verify": []},
    }


def render_default_config(project_name: str = "example-app") -> str:
    return f"""version: {CONFIG_VERSION}

project:
  name: {project_name}

state:
  db: .mergetrain/queue.sqlite
  logs: .mergetrain/logs
  worktree_root: .mergetrain/worktrees
  validation_workspace:
    mode: ephemeral
    cache_key: ""
    cache_paths: []

git:
  remote: origin
  integration_branch: main
  push_refs:
    - main

queue:
  lock_ttl_minutes: 30
  daemon_interval_seconds: 15
  heartbeat_interval_seconds: 10
  command_timeout_seconds: 3600

notify:
  # Optional provider-neutral JSON webhook; this URL may contain a secret.
  webhook_url: ""
  transitions:
    - landed
    - blocked
    - needs_reconcile
    - daemon_paused
  timeout_seconds: 10

gate_parallelism:
  # Sequential by default. Increase only for gates explicitly grouped below.
  max_workers: 1
  # Optional total wall-clock ceiling for the configured gate plan.
  # timeout_seconds: 1800

gates: []
  # mergetrain always runs its built-in diff-check first.
  # Add service-specific checks here, for example:
  # - name: tests
  #   run: python -m unittest discover -s tests

deploy:
  verify: []
  reuse:
    enabled: false
    max_age_minutes: 60
    on_mismatch: rerun
    fingerprints: []
  # Add post-push checks here, for example:
  # verify:
  #   - name: live-health
  #     run: curl -fsS https://example.invalid/health
"""


def _as_mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a mapping")
    return value


def _as_gate_list(
    value: Any,
    *,
    key: str,
    allow_paths: bool = False,
    allow_parallel: bool = False,
) -> tuple[GateConfig, ...]:
    if value in (None, {}):
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{key} must be a list")
    gates: list[GateConfig] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ConfigError(f"{key}[{index}] must be a mapping")
        name_value = item.get("name", "")
        run_value = item.get("run", "")
        if not isinstance(name_value, str) or not isinstance(run_value, str):
            raise ConfigError(f"{key}[{index}] name and run must be strings")
        name = name_value.strip()
        run = run_value.strip()
        if not name or not run:
            raise ConfigError(f"{key}[{index}] requires name and run")
        always_rerun = item.get("always_rerun_on_deploy", False)
        if not isinstance(always_rerun, bool):
            raise ConfigError(
                f"{key}[{index}].always_rerun_on_deploy must be true or false"
            )
        raw_paths = item.get("paths")
        paths: tuple[str, ...] = ()
        if raw_paths is not None:
            if not allow_paths:
                raise ConfigError(f"{key}[{index}].paths is unsupported")
            if not isinstance(raw_paths, list) or not raw_paths:
                raise ConfigError(
                    f"{key}[{index}].paths must be a non-empty list"
                )
            parsed_paths: list[str] = []
            for path_index, pattern in enumerate(raw_paths):
                if not isinstance(pattern, str):
                    raise ConfigError(
                        f"{key}[{index}].paths[{path_index}] must be a string"
                    )
                try:
                    parsed_paths.append(validate_gate_path_pattern(pattern))
                except ValueError as exc:
                    raise ConfigError(
                        f"{key}[{index}].paths[{path_index}] {exc}"
                    ) from exc
            if len(set(parsed_paths)) != len(parsed_paths):
                raise ConfigError(f"{key}[{index}].paths must not contain duplicates")
            paths = tuple(parsed_paths)
        parallel_group = ""
        needs: tuple[str, ...] = ()
        workers = 1
        timeout_seconds: int | None = None
        execution_fields = {
            "parallel_group",
            "needs",
            "workers",
            "timeout_seconds",
        }
        configured_execution_fields = execution_fields.intersection(item)
        if configured_execution_fields and not allow_parallel:
            field = sorted(configured_execution_fields)[0]
            raise ConfigError(f"{key}[{index}].{field} is unsupported")
        if allow_parallel:
            raw_group = item.get("parallel_group", "")
            if not isinstance(raw_group, str):
                raise ConfigError(
                    f"{key}[{index}].parallel_group must be a string"
                )
            parallel_group = raw_group.strip()
            if "parallel_group" in item and not parallel_group:
                raise ConfigError(
                    f"{key}[{index}].parallel_group must be a non-empty string"
                )
            raw_needs = item.get("needs")
            if raw_needs is not None:
                if not isinstance(raw_needs, list) or not raw_needs:
                    raise ConfigError(
                        f"{key}[{index}].needs must be a non-empty list"
                    )
                parsed_needs = [
                    _nonempty_string(
                        dependency,
                        key=f"{key}[{index}].needs[{dependency_index}]",
                    )
                    for dependency_index, dependency in enumerate(raw_needs)
                ]
                if len(set(parsed_needs)) != len(parsed_needs):
                    raise ConfigError(
                        f"{key}[{index}].needs must not contain duplicates"
                    )
                needs = tuple(parsed_needs)
            workers = _positive_int(
                item.get("workers", 1), key=f"{key}[{index}].workers"
            )
            raw_timeout = item.get("timeout_seconds")
            if raw_timeout is not None:
                timeout_seconds = _positive_int(
                    raw_timeout, key=f"{key}[{index}].timeout_seconds"
                )
        gates.append(
            GateConfig(
                name=name,
                run=run,
                always_rerun_on_deploy=always_rerun,
                paths=paths,
                parallel_group=parallel_group,
                needs=needs,
                workers=workers,
                timeout_seconds=timeout_seconds,
            )
        )
    return tuple(gates)


def _nonempty_string(value: Any, *, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string")
    return value.strip()


def _push_refs(value: Any) -> tuple[str, ...]:
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not items:
        raise ConfigError("git.push_refs must contain at least one ref")
    refs: list[str] = []
    for index, item in enumerate(items):
        refs.append(_nonempty_string(item, key=f"git.push_refs[{index}]"))
    if len(set(refs)) != len(refs):
        raise ConfigError("git.push_refs must not contain duplicates")
    return tuple(refs)


def _positive_int(value: Any, *, key: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be a positive integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdecimal():
        parsed = int(value.strip())
    else:
        raise ConfigError(f"{key} must be a positive integer")
    if parsed <= 0:
        raise ConfigError(f"{key} must be a positive integer")
    return parsed


def _boolean(value: Any, *, key: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be true or false")
    return value


def _validation_cache_path(value: Any, *, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty directory path")
    path = value.strip()
    pure = PurePosixPath(path)
    if (
        "\\" in path
        or pure.is_absolute()
        or path.endswith("/")
        or any(part in {"", ".", "..", ".git"} for part in pure.parts)
        or re.match(r"^[A-Za-z]:", path)
    ):
        raise ConfigError(
            f"{key} must be a normalized repository-relative POSIX directory path"
        )
    if any(character in path for character in "*?["):
        raise ConfigError(f"{key} does not support glob patterns")
    return pure.as_posix()


def _shared_state_root(repo: Path) -> Path:
    """Return the control checkout shared by standard linked worktrees.

    A linked worktree stores a ``.git`` file that points at
    ``<common>/.git/worktrees/<name>``. Its ``commondir`` file points back to
    the control checkout's ``.git`` directory. Resolve only that standard
    shape; malformed metadata, submodules, bare repositories, and ordinary
    directories retain the historical repository-relative behavior.
    """

    git_file = repo / ".git"
    if not git_file.is_file():
        return repo
    try:
        marker = git_file.read_text(encoding="utf-8").strip()
        if not marker.startswith("gitdir:"):
            return repo
        git_dir = Path(marker.removeprefix("gitdir:").strip()).expanduser()
        if not git_dir.is_absolute():
            git_dir = repo / git_dir
        git_dir = git_dir.resolve()
        common_value = (git_dir / "commondir").read_text(encoding="utf-8").strip()
        common_git_dir = Path(common_value).expanduser()
        if not common_git_dir.is_absolute():
            common_git_dir = git_dir / common_git_dir
        common_git_dir = common_git_dir.resolve()
    except (OSError, UnicodeError):
        return repo
    if (
        common_git_dir.name != ".git"
        or not common_git_dir.is_dir()
        or git_dir.parent.parent != common_git_dir
    ):
        return repo
    return common_git_dir.parent.resolve()


def _resolve_path(repo: Path, value: Any, default: str, *, key: str) -> Path:
    if value is None:
        raw = default
    elif isinstance(value, (str, Path)):
        raw = str(value)
    else:
        raise ConfigError(f"{key} must be a path string")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = repo / path
    return path.resolve()


def load_config(
    *,
    config_path: str | Path | None = None,
    repo: str | Path | None = None,
    db_override: str | Path | None = None,
) -> MergetrainConfig:
    repo_path = Path(repo or Path.cwd()).expanduser().resolve()
    state_root = _shared_state_root(repo_path)
    path = Path(config_path).expanduser() if config_path else repo_path / DEFAULT_CONFIG_NAME
    if not path.is_absolute():
        path = (repo_path / path).resolve()
    exists = path.exists()
    if exists:
        data = load_yaml(path.read_text(encoding="utf-8"))
    else:
        data = default_config_dict(repo_path.name or "example-app")

    config_version, data = _read_config_version(data)

    project_data = _as_mapping(data, "project")
    project_name_value = project_data.get("name")
    project_name = (
        (repo_path.name or "example-app")
        if project_name_value is None
        else _nonempty_string(project_name_value, key="project.name")
    )

    state_data = _as_mapping(data, "state")
    db_value = db_override if db_override is not None else state_data.get("db")
    workspace_data = state_data.get("validation_workspace", {})
    if workspace_data is None:
        workspace_data = {}
    if not isinstance(workspace_data, dict):
        raise ConfigError("state.validation_workspace must be a mapping")
    workspace_mode = str(workspace_data.get("mode", "ephemeral")).strip()
    if workspace_mode not in {"ephemeral", "persistent"}:
        raise ConfigError(
            "state.validation_workspace.mode must be 'ephemeral' or 'persistent'"
        )
    cache_key_value = workspace_data.get("cache_key", "")
    if not isinstance(cache_key_value, str):
        raise ConfigError("state.validation_workspace.cache_key must be a string")
    cache_key = cache_key_value.strip()
    cache_paths_value = workspace_data.get("cache_paths", [])
    if not isinstance(cache_paths_value, list):
        raise ConfigError("state.validation_workspace.cache_paths must be a list")
    cache_paths = tuple(
        _validation_cache_path(
            value, key=f"state.validation_workspace.cache_paths[{index}]"
        )
        for index, value in enumerate(cache_paths_value)
    )
    if len(set(cache_paths)) != len(cache_paths):
        raise ConfigError(
            "state.validation_workspace.cache_paths must not contain duplicates"
        )
    if workspace_mode == "persistent":
        if not cache_key:
            raise ConfigError(
                "state.validation_workspace.cache_key is required in persistent mode"
            )
        if not cache_paths:
            raise ConfigError(
                "state.validation_workspace.cache_paths must not be empty in persistent mode"
            )
    state = StateConfig(
        db=_resolve_path(
            repo_path if db_override is not None else state_root,
            db_value,
            ".mergetrain/queue.sqlite",
            key="state.db",
        ),
        logs=_resolve_path(
            state_root,
            state_data.get("logs"),
            ".mergetrain/logs",
            key="state.logs",
        ),
        worktree_root=_resolve_path(
            state_root,
            state_data.get("worktree_root"),
            ".mergetrain/worktrees",
            key="state.worktree_root",
        ),
        validation_workspace=ValidationWorkspaceConfig(
            mode=workspace_mode,
            cache_key=cache_key,
            cache_paths=cache_paths,
        ),
    )

    git_data = _as_mapping(data, "git")
    integration_branch = _nonempty_string(
        git_data.get("integration_branch", "main"), key="git.integration_branch"
    )
    push_refs = (
        _push_refs(git_data["push_refs"])
        if "push_refs" in git_data
        else (integration_branch,)
    )
    git = GitConfig(
        remote=_nonempty_string(git_data.get("remote", "origin"), key="git.remote"),
        integration_branch=integration_branch,
        push_refs=push_refs,
    )

    queue_data = _as_mapping(data, "queue")
    lock_ttl_minutes = _positive_int(
        queue_data.get("lock_ttl_minutes", 30), key="queue.lock_ttl_minutes"
    )
    heartbeat_interval_seconds = _positive_int(
        queue_data.get("heartbeat_interval_seconds", 10),
        key="queue.heartbeat_interval_seconds",
    )
    if heartbeat_interval_seconds >= lock_ttl_minutes * 60:
        raise ConfigError(
            "queue.heartbeat_interval_seconds must be shorter than queue.lock_ttl_minutes"
        )
    queue = QueueConfig(
        lock_ttl_minutes=lock_ttl_minutes,
        daemon_interval_seconds=_positive_int(
            queue_data.get("daemon_interval_seconds", 15),
            key="queue.daemon_interval_seconds",
        ),
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        command_timeout_seconds=_positive_int(
            queue_data.get("command_timeout_seconds", 3600),
            key="queue.command_timeout_seconds",
        ),
    )

    notify_data = _as_mapping(data, "notify")
    webhook_value = notify_data.get("webhook_url", "")
    if webhook_value is None:
        webhook_url = ""
    elif isinstance(webhook_value, str):
        webhook_url = webhook_value.strip()
    else:
        raise ConfigError("notify.webhook_url must be a string")
    if webhook_url:
        parsed_webhook = urlsplit(webhook_url)
        if parsed_webhook.scheme not in {"http", "https"} or not parsed_webhook.hostname:
            raise ConfigError("notify.webhook_url must be an http or https URL")
    transitions_value = notify_data.get("transitions", list(NOTIFY_TRANSITIONS))
    if not isinstance(transitions_value, list):
        raise ConfigError("notify.transitions must be a list")
    transitions: list[str] = []
    for index, value in enumerate(transitions_value):
        transition = _nonempty_string(value, key=f"notify.transitions[{index}]")
        if transition not in NOTIFY_TRANSITIONS:
            allowed = ", ".join(NOTIFY_TRANSITIONS)
            raise ConfigError(
                f"notify.transitions[{index}] must be one of: {allowed}"
            )
        if transition not in transitions:
            transitions.append(transition)
    notify = NotifyConfig(
        webhook_url=webhook_url,
        transitions=tuple(transitions),
        timeout_seconds=_positive_int(
            notify_data.get("timeout_seconds", 10),
            key="notify.timeout_seconds",
        ),
    )

    gate_parallelism_data = _as_mapping(data, "gate_parallelism")
    raw_gate_plan_timeout = gate_parallelism_data.get("timeout_seconds")
    gate_parallelism = GateParallelismConfig(
        max_workers=_positive_int(
            gate_parallelism_data.get("max_workers", 1),
            key="gate_parallelism.max_workers",
        ),
        timeout_seconds=(
            _positive_int(
                raw_gate_plan_timeout,
                key="gate_parallelism.timeout_seconds",
            )
            if raw_gate_plan_timeout is not None
            else None
        ),
    )

    deploy_data = _as_mapping(data, "deploy")
    reuse_value = deploy_data.get("reuse", {})
    if reuse_value is None:
        reuse_value = {}
    if not isinstance(reuse_value, dict):
        raise ConfigError("deploy.reuse must be a mapping")
    on_mismatch = str(reuse_value.get("on_mismatch", "rerun")).strip()
    if on_mismatch not in {"rerun", "fail"}:
        raise ConfigError("deploy.reuse.on_mismatch must be 'rerun' or 'fail'")
    fingerprints = _as_gate_list(
        reuse_value.get("fingerprints", []), key="deploy.reuse.fingerprints"
    )
    if any(item.always_rerun_on_deploy for item in fingerprints):
        raise ConfigError(
            "deploy.reuse.fingerprints do not support always_rerun_on_deploy"
        )
    deploy = DeployConfig(
        verify=_as_gate_list(deploy_data.get("verify", []), key="deploy.verify"),
        reuse=ReuseConfig(
            enabled=_boolean(
                reuse_value.get("enabled", False), key="deploy.reuse.enabled"
            ),
            max_age_minutes=_positive_int(
                reuse_value.get("max_age_minutes", 60),
                key="deploy.reuse.max_age_minutes",
            ),
            on_mismatch=on_mismatch,
            fingerprints=fingerprints,
        ),
    )
    gates = _as_gate_list(
        data.get("gates", []),
        key="gates",
        allow_paths=True,
        allow_parallel=True,
    )
    gate_names = [
        gate.name
        for gate in (*gates, *deploy.verify, *deploy.reuse.fingerprints)
    ]
    if len(set(gate_names)) != len(gate_names):
        raise ConfigError(
            "gate, deploy.verify, and deploy.reuse.fingerprint names must be unique"
        )
    configured_names: set[str] = set()
    closed_groups: set[str] = set()
    current_group = ""
    for index, gate in enumerate(gates):
        if gate.parallel_group != current_group:
            if current_group:
                closed_groups.add(current_group)
            current_group = gate.parallel_group
            if current_group and current_group in closed_groups:
                raise ConfigError(
                    f"gates[{index}].parallel_group {current_group!r} must be contiguous"
                )
        for dependency in gate.needs:
            if dependency != "diff-check" and dependency not in configured_names:
                raise ConfigError(
                    f"gates[{index}].needs references {dependency!r}, which must be "
                    "the built-in diff-check or an earlier configured gate"
                )
        if gate.workers > gate_parallelism.max_workers:
            raise ConfigError(
                f"gates[{index}].workers exceeds gate_parallelism.max_workers"
            )
        configured_names.add(gate.name)

    return MergetrainConfig(
        project=ProjectConfig(name=project_name),
        state=state,
        git=git,
        queue=queue,
        notify=notify,
        gates=gates,
        gate_parallelism=gate_parallelism,
        deploy=deploy,
        repo=repo_path,
        config_path=path,
        config_exists=exists,
        config_version=config_version,
    )


def _read_config_version(data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Return ``(config_version, migrated_data)``.

    An absent ``version:`` key means version 1 — every config written before
    versioning existed rides forward unchanged. A newer version is *recorded*,
    not rejected here: refusal is command-scoped (the deploy path fails closed;
    recovery stays permissive), so load_config never blocks. Older versions are
    migrated forward in memory only because re-serializing hand-edited YAML
    would lose comments and unknown keys.
    """

    raw = data.get("version", 1)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(
            f"config 'version' must be an integer, got {raw!r}"
        )
    if raw < 1:
        raise ConfigError(f"config 'version' must be >= 1, got {raw}")
    data = {key: value for key, value in data.items() if key != "version"}
    if raw < CONFIG_VERSION:
        data = _migrate_config(data, from_version=raw)
    elif raw == CONFIG_VERSION:
        removed = sorted({"agent", "terminology"}.intersection(data))
        if removed:
            raise ConfigError(
                "config version 2 removed top-level key(s): "
                + ", ".join(removed)
            )
    return raw, data


def _migrate_config(data: dict[str, Any], *, from_version: int) -> dict[str, Any]:
    """Forward-migrate parsed config data without rewriting hand-edited YAML."""

    migrated = dict(data)
    if from_version < 2:
        migrated.pop("agent", None)
        migrated.pop("terminology", None)
    return migrated
