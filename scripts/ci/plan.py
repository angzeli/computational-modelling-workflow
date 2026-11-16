"""Pure tier selection plus a local Git adapter; no GitHub API or credentials."""
from __future__ import annotations

import argparse
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
POLICY = Path(__file__).with_name('paths.json')
PLATFORMS = [{'os': 'ubuntu-24.04', 'python': '3.11'},
             {'os': 'macos-15', 'python': '3.14'}]


def plan(event_name, event, paths, policy):
    """Fail broad on an empty diff; only explicit prose/site paths are docs-only."""
    def matches(group):
        return any(fnmatchcase(path, pattern) for path in paths for pattern in policy[group])
    docs_only = bool(paths) and all(any(fnmatchcase(path, p) for p in policy['documentation'])
                                    for path in paths)
    pr = event.get('pull_request', {})
    labels = {label['name'] for label in pr.get('labels', [])}
    action = event.get('action', '')
    label = event.get('label', {}).get('name', '')
    code_event = action in {'opened', 'reopened', 'synchronize'}
    ready_event = action == 'ready_for_review' or (action == 'labeled' and label == policy['merge_label'])
    forced = (action == 'labeled' and label == policy['full_label']) or (
        code_event and policy['full_label'] in labels)
    full = event_name in {'push', 'schedule'} or forced or (code_event and matches('full'))
    active = event_name in {'push', 'schedule'} or code_event or ready_event or forced
    fast = full or (code_event and not docs_only)
    merge = full or (not docs_only and (ready_event or
                    (code_event and (not pr.get('draft', True) or policy['merge_label'] in labels))))
    matrix = full or (code_event and matches('fault_matrix'))
    packaging = full or (code_event and matches('packaging'))
    suites = ([PLATFORMS[0]] if fast else []) + ([PLATFORMS[1]] if merge else [])
    artifacts = ([{**p, 'modes': 'all'} for p in PLATFORMS] if packaging else
                 [{**PLATFORMS[0], 'modes': 'core'}] if fast else [])
    return {'active': active, 'docs_only': docs_only, 'full': full,
            'suites': suites, 'sweep': PLATFORMS if matrix else [], 'artifacts': artifacts,
            'paths': sorted(set(paths))}


def changed_paths(event_name, event, root=ROOT):
    if event_name != 'pull_request':
        return []
    pr = event['pull_request']
    base, head = pr['base']['sha'], pr['head']['sha']
    if not all(re.fullmatch(r'[0-9a-f]{40}', ref) for ref in (base, head)):
        raise ValueError('Expected full Git commit IDs')
    # --no-renames includes both old and new paths. NUL delimiters preserve names.
    raw = subprocess.check_output(['git', 'diff', '--name-only', '-z', '--no-renames',
                                   f'{base}...{head}'], cwd=root)
    return [p.decode('utf-8') for p in raw.split(b'\0') if p]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--event', default=os.environ.get('GITHUB_EVENT_PATH'))
    parser.add_argument('--event-name', default=os.environ.get('GITHUB_EVENT_NAME'))
    args = parser.parse_args()
    event = json.loads(Path(args.event).read_text())
    result = plan(args.event_name, event, changed_paths(args.event_name, event),
                  json.loads(POLICY.read_text()))
    print(json.dumps(result, indent=2))
    if output := os.environ.get('GITHUB_OUTPUT'):
        with open(output, 'a') as handle:
            for name, value in result.items():
                if name != 'paths':
                    handle.write(f'{name}={json.dumps(value, separators=(",", ":"))}\n')
    if summary := os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(summary, 'a') as handle:
            handle.write('## CI tier plan\n```json\n' + json.dumps(result, indent=2) + '\n```\n')


if __name__ == '__main__':
    main()
