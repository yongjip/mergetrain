from __future__ import annotations

import contextlib
import io
import re
import shlex
from pathlib import Path

from mergetrain.cli import build_parser
from mergetrain.commands.setup import render_agent_contract


def test_pasted_operator_reference_examples_parse_in_current_cli() -> None:
    reference = render_agent_contract().split('## Current command reference\n', 1)[1]
    reference = reference.split('\n## Rules\n', 1)[0]
    commands = re.findall(r'`(mergetrain [^`]+)`', reference)
    assert commands
    seen = set()
    for command in commands:
        argv = shlex.split(command.replace('JOB_ID', '7'))[1:]
        parser = build_parser()
        if argv == ['--version']:
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    parser.parse_args(argv)
                except SystemExit as exc:
                    assert exc.code == 0
        else:
            namespace = parser.parse_args(argv)
            assert callable(namespace.func)
            seen.add(argv[0])
    assert seen == {'status', 'inspect'}


def test_operator_experiment_does_not_change_discovery_description() -> None:
    root = Path(__file__).resolve().parents[1]
    evidence = root / 'benchmarks/operator_guidance'
    baseline = (evidence / 'baseline-skill.md').read_text()
    candidate = (evidence / 'candidate-skill.md').read_text()
    assert baseline.split('\n---\n', 1)[0] == candidate.split('\n---\n', 1)[0]
