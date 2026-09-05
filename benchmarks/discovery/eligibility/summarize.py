"""Summarize captured skill-body reads; this is not a semantic answer grader."""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path

BODY_MARKER = 'Purpose: Serialize committed local task branches'


def summarize(evidence: Path) -> dict:
    rows = json.loads((evidence / 'results.json').read_text(encoding='utf-8'))
    frozen = json.loads((evidence / 'frozen.json').read_text(encoding='utf-8'))
    # Preserve paired comparison: if either arm consults the ambient, unpinned
    # CLI, exclude BOTH arms from the strict suitable-question stratum.
    ambient = {
        r['id'] for r in rows if any(
            t.get('type') == 'command_execution'
            and 'mergetrain ' in t.get('command', '')
            and 'command -v' not in t.get('command', '')
            and 'SKILL.md' not in t.get('command', '')
            and not any(x in t.get('command', '') for x in ('rg ', 'find '))
            for t in r['tool_items']
        )
    }
    records = []
    for row in rows:
        body_read = any(BODY_MARKER in t.get('aggregated_output', '')
                        for t in row['tool_items'])
        records.append({
            'id': row['id'], 'group': row['group'], 'arm': row['arm'],
            'complete': row['complete'], 'skill_body_read': body_read,
            'strict_pair_excluded': row['id'] in ambient,
            'workspace_unchanged': row['workspace_unchanged'],
            'elapsed_seconds': row['elapsed_seconds'], 'usage': row['usage'],
            'record_directory': Path(row['record']).name,
            'stdout_sha256': row['stdout_sha256'], 'stderr_sha256': row['stderr_sha256'],
        })
    metrics = {}
    for arm in ('baseline', 'candidate'):
        metrics[arm] = {}
        for group in ('suitable', 'negative', 'boundary'):
            all_rows = [r for r in records if r['arm'] == arm and r['group'] == group]
            strict = [r for r in all_rows if r['complete'] and not r['strict_pair_excluded']]
            metrics[arm][group] = {
                'raw_complete': sum(r['complete'] for r in all_rows),
                'raw_skill_body_reads': sum(r['skill_body_read'] for r in all_rows),
                'strict_pair_denominator': len(strict),
                'strict_skill_body_reads': sum(r['skill_body_read'] for r in strict),
                'raw_median_wall_seconds': statistics.median(
                    r['elapsed_seconds'] for r in all_rows),
                'raw_median_output_tokens': statistics.median(
                    r['usage']['output_tokens'] for r in all_rows if r['usage']),
            }
    return {
        'schema_version': 1,
        'scope': frozen['scope'],
        'client': frozen['client'], 'model': frozen['model'], 'reasoning': frozen['reasoning'],
        'source_commit': frozen['source_commit'],
        'fixtures_sha256': frozen['fixtures_sha256'], 'arms_sha256': frozen['arms_sha256'],
        'runner_sha256': frozen['runner_sha256'],
        'frozen_file_sha256': hashlib.sha256((evidence / 'frozen.json').read_bytes()).hexdigest(),
        'review': 'author trace review; not independent human benchmark approval',
        'complete_runs': sum(r['complete'] for r in records),
        'unchanged_workspaces': sum(r['workspace_unchanged'] for r in records),
        'excluded_pairs': sorted(ambient), 'metrics': metrics,
        'decision': ('reject candidate; keep released metadata'
                     if metrics['candidate']['negative']['strict_skill_body_reads'] > 1
                     else 'diagnostic only; no promotion without complete independent validation'),
        'records': sorted(records, key=lambda r: (r['id'], r['arm'])),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = summarize(args.evidence)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('complete_runs', 'excluded_pairs', 'metrics')},
                     indent=2))


if __name__ == '__main__':
    main()
