#!/usr/bin/env python3
"""Enforce mergetrain's coarse module dependency and monolith boundaries."""

from __future__ import annotations

import argparse
import ast
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

PACKAGE = "mergetrain"
PYTHON_MAX_LINES = 2_500
PYTHON_MAX_BYTES = 100_000
FRONTEND_MAX_LINES = 1_200
FRONTEND_MAX_BYTES = 50_000
FRONTEND_SUFFIXES = {".css", ".js", ".jsx", ".mjs", ".ts", ".tsx"}

CLI_PREFIXES = (
    "mergetrain.__main__",
    "mergetrain.cli",
    "mergetrain.cli_support",
    "mergetrain.commands",
)
PERSISTENCE_ALLOWED = (
    "mergetrain.errors",
    "mergetrain.models",
    "mergetrain.persistence",
)
STORE_ALLOWED = ("mergetrain.persistence",)
GIT_RUNNER_COLLABORATORS = {
    "mergetrain.atomic_push",
    "mergetrain.command_runner",
    "mergetrain.gate_runner",
    "mergetrain.git_ops",
    "mergetrain.reuse",
    "mergetrain.validation_reuse",
    "mergetrain.worktree_manager",
}
ADAPTER_ALLOWED = {
    "mergetrain.mcp_server": (
        "mergetrain.contract",
        "mergetrain.errors",
    ),
    "mergetrain.dashboard": (
        "mergetrain.config",
        "mergetrain.contract",
        "mergetrain.errors",
        "mergetrain.hub",
        "mergetrain.snapshot",
    ),
    "mergetrain.hub": (
        "mergetrain.config",
        "mergetrain.registry",
        "mergetrain.snapshot",
        "mergetrain.store",
    ),
}


@dataclass(frozen=True)
class ImportEdge:
    source: str
    target: str
    path: str
    line: int


@dataclass(frozen=True, order=True)
class Violation:
    path: str
    line: int
    rule: str
    detail: str


@dataclass(frozen=True)
class ArchitectureReport:
    checked_python_files: int
    checked_frontend_files: int
    internal_edges: int
    violations: tuple[Violation, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": not self.violations,
            "checked_python_files": self.checked_python_files,
            "checked_frontend_files": self.checked_frontend_files,
            "internal_edges": self.internal_edges,
            "violations": [asdict(item) for item in self.violations],
        }


def _matches(module: str, prefixes: Iterable[str]) -> bool:
    return any(module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes)


def _module_paths(repo: Path) -> dict[str, Path]:
    source_root = repo / "src"
    package_root = source_root / PACKAGE
    result: dict[str, Path] = {}
    for path in sorted(package_root.rglob("*.py")):
        parts = list(path.relative_to(source_root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        result[".".join(parts)] = path
    return result


def _relative_target(
    source: str, level: int, module: str | None, *, is_package: bool
) -> str:
    parent = source.split(".") if is_package else source.split(".")[:-1]
    trim = max(0, level - 1)
    base = parent[: len(parent) - trim] if trim else parent
    if module:
        base.extend(module.split("."))
    return ".".join(base)


def _import_edges(
    module: str,
    path: Path,
    *,
    repo: Path,
    known_modules: set[str],
) -> list[ImportEdge]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    display_path = path.relative_to(repo).as_posix()
    edges: list[ImportEdge] = []
    for node in ast.walk(tree):
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _relative_target(
                    module,
                    node.level,
                    node.module,
                    is_package=path.name == "__init__.py",
                )
            else:
                base = node.module or ""
            targets.append(base)
            if node.module in {None, PACKAGE}:
                for alias in node.names:
                    candidate = f"{base}.{alias.name}" if base else alias.name
                    if candidate in known_modules:
                        targets.append(candidate)
        for target in targets:
            if target == PACKAGE or target.startswith(f"{PACKAGE}."):
                edges.append(
                    ImportEdge(
                        source=module,
                        target=target,
                        path=display_path,
                        line=node.lineno,
                    )
                )
    return edges


def _dependency_violations(edges: list[ImportEdge]) -> list[Violation]:
    violations: list[Violation] = []
    for edge in edges:
        if not _matches(edge.source, CLI_PREFIXES) and _matches(edge.target, CLI_PREFIXES):
            violations.append(
                Violation(
                    edge.path,
                    edge.line,
                    "core-must-not-import-cli",
                    f"{edge.source} imports outer CLI module {edge.target}",
                )
            )

        if (
            _matches(edge.source, ("mergetrain.commands",))
            and _matches(edge.target, ("mergetrain.commands",))
            and edge.source != edge.target
        ):
            violations.append(
                Violation(
                    edge.path,
                    edge.line,
                    "commands-must-not-call-each-other",
                    f"{edge.source} imports sibling command module {edge.target}",
                )
            )

        if _matches(edge.source, ("mergetrain.persistence",)) and not _matches(
            edge.target, PERSISTENCE_ALLOWED
        ):
            violations.append(
                Violation(
                    edge.path,
                    edge.line,
                    "persistence-dependency-direction",
                    f"{edge.source} may depend only on persistence primitives, models, and errors; found {edge.target}",
                )
            )

        if edge.source == "mergetrain.store" and not _matches(edge.target, STORE_ALLOWED):
            violations.append(
                Violation(
                    edge.path,
                    edge.line,
                    "store-is-a-compatibility-facade",
                    f"store.py may re-export persistence APIs but must not own policy via {edge.target}",
                )
            )

        if edge.source in GIT_RUNNER_COLLABORATORS and _matches(
            edge.target, ("mergetrain.git_runner",)
        ):
            violations.append(
                Violation(
                    edge.path,
                    edge.line,
                    "git-runner-dependency-direction",
                    f"{edge.source} is a narrow collaborator and must not import coordinator {edge.target}",
                )
            )

        allowed = ADAPTER_ALLOWED.get(edge.source)
        if allowed is not None and not _matches(edge.target, allowed):
            violations.append(
                Violation(
                    edge.path,
                    edge.line,
                    "adapter-must-stay-thin",
                    f"{edge.source} may use only its documented read/contract dependencies; found {edge.target}",
                )
            )
    return violations


def _cycle_violations(
    edges: list[ImportEdge], module_paths: dict[str, Path], *, repo: Path
) -> list[Violation]:
    modules = set(module_paths)
    graph = {module: set() for module in modules}
    edge_by_pair: dict[tuple[str, str], ImportEdge] = {}
    for edge in edges:
        if edge.target in modules and edge.target != edge.source:
            graph[edge.source].add(edge.target)
            edge_by_pair.setdefault((edge.source, edge.target), edge)

    state: dict[str, int] = {}
    stack: list[str] = []
    cycles: set[tuple[str, ...]] = set()

    def visit(module: str) -> None:
        state[module] = 1
        stack.append(module)
        for target in sorted(graph[module]):
            if state.get(target, 0) == 0:
                visit(target)
            elif state.get(target) == 1:
                cycle = stack[stack.index(target) :]
                rotations = [tuple(cycle[index:] + cycle[:index]) for index in range(len(cycle))]
                cycles.add(min(rotations))
        stack.pop()
        state[module] = 2

    for module in sorted(modules):
        if state.get(module, 0) == 0:
            visit(module)

    violations: list[Violation] = []
    for cycle in sorted(cycles):
        closing = edge_by_pair[(cycle[-1], cycle[0])]
        path = module_paths[cycle[-1]].relative_to(repo).as_posix()
        route = " -> ".join((*cycle, cycle[0]))
        violations.append(
            Violation(path, closing.line, "no-internal-import-cycles", route)
        )
    return violations


def _size_violation(
    path: Path,
    *,
    repo: Path,
    max_lines: int,
    max_bytes: int,
    rule: str,
) -> Violation | None:
    payload = path.read_bytes()
    line_count = len(payload.splitlines())
    if line_count <= max_lines and len(payload) <= max_bytes:
        return None
    return Violation(
        path.relative_to(repo).as_posix(),
        1,
        rule,
        f"{line_count} lines/{len(payload)} bytes exceeds coarse backstop "
        f"of {max_lines} lines/{max_bytes} bytes; split responsibilities before adding more",
    )


def check_repository(repo: Path) -> ArchitectureReport:
    repo = repo.resolve()
    module_paths = _module_paths(repo)
    known_modules = set(module_paths)
    edges: list[ImportEdge] = []
    violations: list[Violation] = []
    for module, path in sorted(module_paths.items()):
        try:
            edges.extend(
                _import_edges(
                    module,
                    path,
                    repo=repo,
                    known_modules=known_modules,
                )
            )
        except SyntaxError as exc:
            violations.append(
                Violation(
                    path.relative_to(repo).as_posix(),
                    exc.lineno or 1,
                    "parse-production-module",
                    exc.msg,
                )
            )

    violations.extend(_dependency_violations(edges))
    violations.extend(_cycle_violations(edges, module_paths, repo=repo))
    for path in module_paths.values():
        violation = _size_violation(
            path,
            repo=repo,
            max_lines=PYTHON_MAX_LINES,
            max_bytes=PYTHON_MAX_BYTES,
            rule="python-production-monolith",
        )
        if violation:
            violations.append(violation)

    frontend_root = repo / "dashboard" / "src"
    frontend_files = [
        path
        for path in sorted(frontend_root.rglob("*"))
        if path.is_file() and path.suffix in FRONTEND_SUFFIXES
    ] if frontend_root.is_dir() else []
    for path in frontend_files:
        violation = _size_violation(
            path,
            repo=repo,
            max_lines=FRONTEND_MAX_LINES,
            max_bytes=FRONTEND_MAX_BYTES,
            rule="frontend-production-monolith",
        )
        if violation:
            violations.append(violation)

    unique_edges = {(edge.source, edge.target, edge.path, edge.line) for edge in edges}
    return ArchitectureReport(
        checked_python_files=len(module_paths),
        checked_frontend_files=len(frontend_files),
        internal_edges=len(unique_edges),
        violations=tuple(sorted(set(violations))),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to the script's parent repository)",
    )
    parser.add_argument("--json", action="store_true", help="emit a machine-readable report")
    args = parser.parse_args(argv)
    report = check_repository(args.repo)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    elif report.violations:
        print("architecture guardrails failed:")
        for item in report.violations:
            print(f"  {item.path}:{item.line}: [{item.rule}] {item.detail}")
    else:
        print(
            "architecture guardrails passed: "
            f"{report.checked_python_files} Python modules, "
            f"{report.checked_frontend_files} frontend files, "
            f"{report.internal_edges} internal import edges"
        )
    return 1 if report.violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
