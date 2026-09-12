"""Copied outside the checkout to test a fresh installation and owned fake jobs.

Only the test launcher accompanies this file. Its observers use fixture PIDs and
fixed idle host samples; every cmw module comes from the installation under test.
"""
import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

import cmw
import cmw.adapters.hof.execution
import cmw.structure.conversion
from cmw.jobs import runtime
from cmw.jobs.ownership import owner_alive
from cmw.jobs.store import ACTIVE, Store
from tests.jobs.isolated_runtime import environment, install
from tests.jobs.lifecycle_evidence import capture


def wait_for(predicate, seconds=12):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('Installed synthetic lifecycle timed out')


def command(argv, expected=0):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=15, env=environment())
    assert result.returncode == expected, (argv, result.returncode, result.stderr[-4000:])
    return result.stdout+result.stderr


def lifecycle(root, mode):
    root.mkdir()
    store = Store(root/'state')
    gates, owners = [], []
    started = time.time()
    with install():
        try:
            if mode == 'bounded-sharing':
                store.configure_sharing(mode, cpu_budget=2, memory_gib=2,
                                        cpu_reserve=0, min_available_gib=0)
            for index in range(2):
                cwd = root/str(index)
                cwd.mkdir()
                gate, ready = cwd/'release', cwd/'ready'
                gates.append(gate)
                code = ("from pathlib import Path; import time; "
                        f"Path({str(ready)!r}).touch(); gate=Path({str(gate)!r}); "
                        "deadline=time.monotonic()+15\n"
                        "while not gate.exists() and time.monotonic()<deadline: time.sleep(.02)\n"
                        "assert gate.exists(), 'fixture deadline exceeded'\n")
                declaration = {}
                if mode == 'bounded-sharing':
                    declaration = ({'role': 'auxiliary', 'independent': True,
                                    'resource_contract': 'trusted-declared'} if index
                                   else {'allow_auxiliary': True})
                store.add(argv=[sys.executable, '-c', code], cwd=cwd,
                          name=f'installed-synthetic-{index}', cpus=1, memory_gib=1, **declaration)
            runtime.start(store)
            wait_for(lambda: (root/'0/ready').exists())
            if mode == 'bounded-sharing':
                wait_for(lambda: (root/'1/ready').exists())
                assert [job['status'] for job in store.snapshot()['jobs']] == ['Run', 'Run']
            else:
                assert not (root/'1/ready').exists()
                assert store.snapshot()['jobs'][1]['status'] == 'Queue'
            for gate in gates:
                gate.touch()
            wait_for(lambda: all(job['status'] == 'Done' for job in store.snapshot()['jobs']))
            state = store.snapshot()
            if mode == 'sequential':
                assert state['jobs'][0]['finished_at'] <= state['jobs'][1]['started_at']
            assert state['schema'] == 2
        except BaseException as exc:
            try:
                capture(store, root/'failure', outcome=str(exc), started_at=started)
            except Exception as capture_error:
                print(f'Additional failure capture unavailable: {capture_error}', file=sys.stderr)
            raise
        finally:
            for gate in gates:
                gate.touch()
            if store.path.exists():
                store.dispatch(False)
                state = store.snapshot()
                owners = [state['controller'].get('owner')]
                for job in state['jobs']:
                    owners.extend((job.get('worker'), job.get('group')))
                try:
                    wait_for(lambda: not any(job['status'] in ACTIVE for job in store.snapshot()['jobs']))
                finally:
                    state = store.snapshot()
                    owners.append(state['controller'].get('owner'))
                    for job in state['jobs']:
                        owners.extend((job.get('worker'), job.get('group')))
                    runtime.stop(store)
                    try:
                        for process in list(runtime._CHILDREN):
                            process.wait(timeout=8)
                    finally:
                        runtime.reap_detached()
                        wait_for(lambda: not any(owner_alive(owner) for owner in owners if owner))
                assert not store.snapshot()['controller']['online']
                assert not runtime._CHILDREN
    return {'mode': mode, 'statuses': ['Done', 'Done'], 'live_recorded_owners_after_cleanup': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=('core', 'jobs', 'sdist', 'editable'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-root', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    origin = Path(cmw.__file__).resolve()
    expected = args.source_root.resolve()/'src' if args.kind == 'editable' else Path(sys.prefix).resolve()
    assert origin.is_relative_to(expected), (origin, expected)
    assert not Path.cwd().resolve().is_relative_to(args.source_root.resolve())
    assert Path(runtime.__file__).with_name('payload.sh').is_file()
    assert 'textual' not in sys.modules
    assert (importlib.util.find_spec('textual') is not None) == (args.kind != 'core')
    cli = str(Path(sys.executable).parent/'cmw')
    assert 'jobs' in command([cli, '--help'])
    assert 'Bounded Sharing' in command([cli, 'jobs', '--help'])
    missing = args.output/'missing-state'
    fixture_cli = [sys.executable, '-m', 'tests.jobs.isolated_runtime', 'cli',
                   'jobs', '--state', str(missing)]
    assert 'CMW / JOBS' in command([*fixture_cli, 'status'])
    state = json.loads(command([*fixture_cli, 'status', '--json']))
    assert not state['jobs'] and not missing.exists()
    if args.kind == 'core':
        assert 'Console dependency missing' in command([*fixture_cli, 'watch'], expected=2)
    else:
        from cmw.jobs.tui import JobsApp
        async def tui():
            with patch.object(JobsApp, 'collect_activity'):
                app = JobsApp(Store(missing))
                async with app.run_test(size=(100, 32)) as pilot:
                    await pilot.pause()
                    assert app.query_one('#table') is not None
        asyncio.run(tui())
        assert not missing.exists()
    report = {'kind': args.kind, 'python': sys.version, 'package_origin': str(origin),
              'cli_json_asset_optional_boundary': 'PASS',
              'tui': 'not installed (expected)' if args.kind == 'core' else 'PASS',
              'lifecycle': [lifecycle(args.output/mode, mode)
                            for mode in ('sequential', 'bounded-sharing')]}
    (args.output/'result.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
