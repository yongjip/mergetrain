from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from benchmarks.discovery.eligibility.policy import Context, Decision, decide


def test_exhaustive_precedence_and_unknown_evidence() -> None:
    seen = 0
    for flags in itertools.product((False, True), repeat=4):
        for facts in itertools.product((False, None, True), repeat=3):
            c = Context(*flags, *facts)
            result = decide(c)
            seen += 1
            # Independent eligibility formula; exclusion and unknown are distinct.
            eligible = not c.prohibited and (
                c.explicit_product_task or c.existing_queue_task or (
                    not c.hosted_queue_task and all(v is True for v in facts)
                )
            )
            unknown = (
                not any(flags) and None in facts and False not in facts
            )
            assert (result == Decision.SELECT) == eligible
            assert (result == Decision.UNRESOLVED) == unknown
    assert seen == 432


def test_existing_queue_and_explicit_requests_override_adoption_conditions() -> None:
    for task in ({'existing_queue_task': True}, {'explicit_product_task': True}):
        assert decide(Context(**task, hosted_queue_task=True,
                              local_workflow=False, parallel_agent_branches=False,
                              integration_need=False)) == Decision.SELECT
        assert decide(Context(**task, prohibited=True)) == Decision.EXCLUDE


def test_unknown_is_not_a_reason_to_invent_fit() -> None:
    assert decide(Context(integration_need=True)) == Decision.UNRESOLVED
    assert decide(Context(local_workflow=True, parallel_agent_branches=False,
                          integration_need=True)) == Decision.EXCLUDE
    assert decide(Context(hosted_queue_task=True, local_workflow=True,
                          parallel_agent_branches=True,
                          integration_need=True)) == Decision.EXCLUDE


def test_treatment_preserves_operational_body_and_distinct_prompt_denominators() -> None:
    root = Path(__file__).resolve().parents[1]
    artifacts = root / 'benchmarks/discovery/eligibility'
    candidate = (artifacts / 'candidate-skill.md').read_text()
    released = (artifacts / 'baseline-skill.md').read_text()
    assert candidate.split('\n---\n', 1)[1] == released.split('\n---\n', 1)[1]
    fixtures = json.loads((artifacts / 'fixtures.json').read_text())['fixtures']
    assert len({f['prompt'] for f in fixtures}) == 48
    assert len({f['id'] for f in fixtures}) == 48
    assert [sum(f['group'] == group for f in fixtures)
            for group in ('suitable', 'negative', 'boundary')] == [20, 20, 8]
    assert all('mergetrain' not in f['prompt'].lower()
               for f in fixtures if f['group'] != 'boundary')


@pytest.mark.parametrize("runner_module", [
    "benchmarks.discovery.eligibility.run",
    "benchmarks.operator_guidance.run",
])
def test_diagnostic_keeps_labels_outside_child_workspace_and_retains_failed_trace(
    tmp_path, monkeypatch, runner_module,
) -> None:
    import importlib
    import subprocess
    import sys
    import tempfile

    run_trial = importlib.import_module(runner_module).run_trial
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    binary = tmp_path / 'codex'
    binary.write_text(
        f'#!{sys.executable}\n'
        'import json, pathlib, sys\n'
        'workspace = pathlib.Path(sys.argv[sys.argv.index("--cd") + 1])\n'
        'files = [p.relative_to(workspace).as_posix() for p in workspace.rglob("*") '
        'if p.is_file()]\n'
        'print(json.dumps({"type":"item.completed","item":{'
        '"type":"command_execution","command":"cat SKILL.md",'
        '"aggregated_output":json.dumps({"argv":sys.argv[1:],"files":files}),'
        '"exit_code":1}}))\n'
        'print(json.dumps({"type":"error","message":"synthetic provider failure"}))\n'
        'sys.exit(1)\n'
    )
    original_popen = subprocess.Popen

    def launch_fixture(command, **kwargs):
        assert command[0] == "codex"
        return original_popen([sys.executable, str(binary), *command[1:]], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch_fixture)
    record = run_trial(
        {'id': 'private-expected-label', 'group': 'negative', 'expected': 'exclude',
         'prompt': 'Explain a workflow.', 'family': 'boundary'},
        'candidate', '---\nname: mergetrain\ndescription: test\n---\n', [], tmp_path,
    )
    assert Path(record['workspace']).parent == scratch
    assert record['exit_code'] == 1
    assert not record['complete']
    assert record['usage'] is None
    assert record['workspace_unchanged']
    assert len(record['tool_items']) == 1
    child = json.loads(record['tool_items'][0]['aggregated_output'])
    assert child['files'] == ['.agents/skills/mergetrain/SKILL.md']
    assert 'private-expected-label' not in json.dumps(child)
    assert 'candidate' not in child['argv']
    assert child['argv'][child['argv'].index('--sandbox') + 1] == 'read-only'
    assert Path(record['record'], 'stdout.jsonl').is_file()


def test_measured_artifacts_keep_frozen_inputs_and_paired_exclusions() -> None:
    import hashlib

    root = Path(__file__).resolve().parents[1] / 'benchmarks/discovery/eligibility'
    result = json.loads((root / 'results-2026-09-05.json').read_text())
    assert result['fixtures_sha256'] == hashlib.sha256(
        (root / 'fixtures.json').read_bytes()).hexdigest()
    for arm in ('baseline', 'candidate'):
        assert result['arms_sha256'][arm] == hashlib.sha256(
            (root / f'{arm}-skill.md').read_bytes()).hexdigest()
    records = result['records']
    assert len(records) == len({(r['id'], r['arm']) for r in records}) == 96
    for row in records:
        assert row['strict_pair_excluded'] == (row['id'] in result['excluded_pairs'])
        assert '/' not in row['record_directory']
    for arm, groups in result['metrics'].items():
        for group, metric in groups.items():
            rows = [r for r in records if r['arm'] == arm and r['group'] == group]
            strict = [r for r in rows if r['complete'] and not r['strict_pair_excluded']]
            assert metric['strict_pair_denominator'] == len(strict)
            assert metric['strict_skill_body_reads'] == sum(r['skill_body_read'] for r in strict)
