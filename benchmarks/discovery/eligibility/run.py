"""Paired, read-only local-skill diagnostic; raw evidence stays outside Git."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree_state(workspace: Path) -> dict[str, str]:
    return {str(p.relative_to(workspace)): digest(p.read_bytes())
            for p in workspace.rglob('*') if p.is_file()}


def run_trial(fixture: dict, arm: str, skill: str, config: list[str], output: Path) -> dict:
    workspace = Path(tempfile.mkdtemp(prefix='mt-e-', dir='/private/tmp'))
    record = output / workspace.name
    record.mkdir()
    target = workspace / '.agents/skills/mergetrain/SKILL.md'
    target.parent.mkdir(parents=True)
    target.write_text(skill)
    before = tree_state(workspace)
    command = [
        'codex', 'exec', '--ignore-user-config', '--ignore-rules', '--ephemeral',
        '--skip-git-repo-check', '--sandbox', 'read-only', '--json', '--color', 'never',
        '--model', 'gpt-5.6-sol', '-c', 'model_reasoning_effort="high"',
        '--cd', str(workspace),
    ]
    for value in config:
        command += ['-c', value]
    command.append(fixture['prompt'])
    (record / 'input.json').write_text(json.dumps({
        'fixture': fixture, 'arm': arm, 'workspace': str(workspace),
        'skill_sha256': digest(skill.encode()),
        'permission_profile': 'read-only; never-approve; ephemeral; ignore-user-config/rules',
    }, ensure_ascii=False, indent=2))
    started = time.monotonic()
    timeout = False
    with (record / 'stdout.jsonl').open('w') as out, (record / 'stderr.txt').open('w') as err:
        proc = subprocess.Popen(command, stdout=out, stderr=err, start_new_session=True)
        try:
            code = proc.wait(timeout=180)
        except subprocess.TimeoutExpired:
            timeout = True
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                code = proc.wait()
    elapsed = time.monotonic() - started
    events = []
    for line in (record / 'stdout.jsonl').read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    items = [e['item'] for e in events if e.get('type') == 'item.completed']
    tools = [i for i in items if i.get('type') != 'agent_message']
    answer = '\n'.join(i.get('text', '') for i in items if i.get('type') == 'agent_message')
    usage = next((e.get('usage') for e in reversed(events)
                  if e.get('type') == 'turn.completed'), None)
    result = {
        'id': fixture['id'], 'group': fixture['group'], 'expected': fixture['expected'],
        'arm': arm, 'record': str(record), 'workspace': str(workspace),
        'exit_code': code, 'timeout': timeout, 'elapsed_seconds': round(elapsed, 3),
        'usage': usage, 'workspace_unchanged': before == tree_state(workspace),
        'tool_items': tools, 'answer': answer,
        'complete': code == 0 and usage is not None and not timeout,
        'stdout_sha256': digest((record / 'stdout.jsonl').read_bytes()),
        'stderr_sha256': digest((record / 'stderr.txt').read_bytes()),
    }
    (record / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-overrides', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    config = json.loads(args.config_overrides.read_text())
    fixtures = json.loads((HERE / 'fixtures.json').read_text())['fixtures']
    arms = {
        'baseline': (ROOT / 'plugins/mergetrain/skills/mergetrain/SKILL.md').read_text(),
        'candidate': (HERE / 'candidate-skill.md').read_text(),
    }
    frozen = {
        'client': subprocess.check_output(['codex', '--version'], text=True).strip(),
        'model': 'gpt-5.6-sol', 'reasoning': 'high', 'seed': 305,
        'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                                                 text=True).strip(),
        'fixtures_sha256': digest((HERE / 'fixtures.json').read_bytes()),
        'arms_sha256': {k: digest(v.encode()) for k, v in arms.items()},
        'runner_sha256': digest(Path(__file__).read_bytes()),
        'config_overrides': config, 'scope': 'isolated native local-skill selection',
    }
    (args.output / 'frozen.json').write_text(json.dumps(frozen, indent=2))
    rng = random.Random(305)
    rng.shuffle(fixtures)
    plans = []
    for fixture in fixtures:
        order = list(arms)
        rng.shuffle(order)
        plans.append((fixture, order))

    def pair(plan: tuple) -> list[dict]:
        fixture, order = plan
        return [run_trial(fixture, arm, arms[arm], config, args.output) for arm in order]

    completed = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(pair, plan) for plan in plans]
        for future in as_completed(futures):
            completed.extend(future.result())
            (args.output / 'results.json').write_text(json.dumps(completed, ensure_ascii=False,
                                                               indent=2))
            print(json.dumps({'finished': len(completed), 'total': 96,
                              'complete': sum(r['complete'] for r in completed)}), flush=True)


if __name__ == '__main__':
    main()
