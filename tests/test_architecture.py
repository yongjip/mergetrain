from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_checker() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "check_architecture.py"
    spec = importlib.util.spec_from_file_location("check_architecture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHECKER = _load_checker()


def _write(repo: Path, relative: str, content: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_repository_obeys_architecture_guardrails() -> None:
    report = CHECKER.check_repository(Path(__file__).resolve().parents[1])
    assert report.violations == ()


def test_forbidden_layer_edges_and_cycles_are_reported(tmp_path: Path) -> None:
    _write(tmp_path, "src/mergetrain/__init__.py", "")
    _write(tmp_path, "src/mergetrain/errors.py", "")
    _write(tmp_path, "src/mergetrain/models.py", "")
    _write(tmp_path, "src/mergetrain/cli.py", "")
    _write(tmp_path, "src/mergetrain/git_runner.py", "")
    _write(tmp_path, "src/mergetrain/store.py", "from .models import Job\n")
    _write(tmp_path, "src/mergetrain/atomic_push.py", "from .git_runner import GitRunner\n")
    _write(tmp_path, "src/mergetrain/dashboard.py", "from .atomic_push import deploy\n")
    _write(tmp_path, "src/mergetrain/core_a.py", "from . import core_b\n")
    _write(tmp_path, "src/mergetrain/core_b.py", "from . import core_a\n")
    _write(tmp_path, "src/mergetrain/persistence/__init__.py", "")
    _write(tmp_path, "src/mergetrain/persistence/jobs.py", "from ..cli import main\n")
    _write(tmp_path, "src/mergetrain/commands/__init__.py", "")
    _write(tmp_path, "src/mergetrain/commands/a.py", "from .b import command\n")
    _write(tmp_path, "src/mergetrain/commands/b.py", "command = object()\n")
    _write(tmp_path, "src/mergetrain/mcp_server.py", "from .git_runner import GitRunner\n")

    report = CHECKER.check_repository(tmp_path)
    rules = {item.rule for item in report.violations}
    assert "core-must-not-import-cli" in rules
    assert "persistence-dependency-direction" in rules
    assert "commands-must-not-call-each-other" in rules
    assert "store-is-a-compatibility-facade" in rules
    assert "git-runner-dependency-direction" in rules
    assert "adapter-must-stay-thin" in rules
    assert "no-internal-import-cycles" in rules


def test_coarse_size_backstops_catch_a_return_to_monoliths(tmp_path: Path) -> None:
    _write(tmp_path, "src/mergetrain/__init__.py", "")
    _write(
        tmp_path,
        "src/mergetrain/oversized.py",
        "x = '" + ("a" * CHECKER.PYTHON_MAX_BYTES) + "'\n",
    )
    _write(
        tmp_path,
        "dashboard/src/App.jsx",
        "\n".join("export const value = 1;" for _ in range(CHECKER.FRONTEND_MAX_LINES + 1)),
    )

    report = CHECKER.check_repository(tmp_path)
    rules = {item.rule for item in report.violations}
    assert "python-production-monolith" in rules
    assert "frontend-production-monolith" in rules
