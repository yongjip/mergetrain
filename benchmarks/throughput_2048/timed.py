"""Record bounded local benchmark commands without changing their exit status."""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('evidence', type=Path)
    parser.add_argument('label')
    parser.add_argument('cwd', type=Path)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0] == '--':
        command = command[1:]
    if not command or not args.label.replace('-', '').replace('_', '').isalnum():
        parser.error('provide a command and an alphanumeric label')
    args.evidence.mkdir(parents=True, exist_ok=True)
    stamp = time.time_ns()
    prefix = args.evidence / f'{args.label}-{stamp}'
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    started = time.monotonic()
    timeout = False
    with prefix.with_suffix('.log').open('w', encoding='utf-8') as output:
        try:
            result = subprocess.run(command, cwd=args.cwd, stdout=output,
                                    stderr=subprocess.STDOUT, timeout=600)
            code = result.returncode
        except subprocess.TimeoutExpired:
            timeout, code = True, 124
    record = {'label': args.label, 'started_at': started_at,
              'elapsed_seconds': time.monotonic() - started,
              'command': command, 'cwd': str(args.cwd),
              'exit_code': code, 'timeout': timeout,
              'log': prefix.with_suffix('.log').name}
    prefix.with_suffix('.json').write_text(json.dumps(record, indent=2) + '\n',
                                          encoding='utf-8')
    print(json.dumps(record))
    print(prefix.with_suffix('.log').read_text(encoding='utf-8'))
    raise SystemExit(code)


if __name__ == '__main__':
    main()
