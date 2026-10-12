"""Installed-artifact acceptance through the real worker, runner, CLI and portal.

The only process-observation replacement is the existing owned-fixture collector.
The computational program is a low-load Python counter with an in-memory sentinel.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

import psutil

from cmw.jobs import runtime, suspension
from cmw.jobs.ownership import identity, owner_alive
from cmw.jobs.store import ACTIVE, Store
from tests.jobs.isolated_runtime import environment, install
from tests.jobs.lifecycle_evidence import capture


def wait_for(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('Installed suspension gate timed out')


def command(argv, expected=0):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=15, env=environment())
    assert result.returncode == expected, (result.returncode, result.stdout[-3000:], result.stderr[-3000:])
    return json.loads(result.stdout)


def suspension_surface(root, *, console):
    root = Path(root)
    root.mkdir(parents=True)
    store = Store(root/'state')
    cli = [sys.executable, '-m', 'tests.jobs.isolated_runtime', 'cli', 'jobs', '--state', str(store.root)]
    owners, transitions = [], []
    started = time.time()
    outcome = 'PASS'

    def current(job_id):
        return next(job for job in store.snapshot()['jobs'] if job['id'] == job_id)

    def ready(job_id, action='suspend'):
        job = current(job_id)
        if job['status'] in {'Fail', 'Unknown', 'Cancelled'} or job['suspension']['phase'] == 'Unverified':
            raise AssertionError(json.dumps(job, indent=2))
        return suspension.readiness(store, job_id, action)['ready']

    def phase(job_id, expected):
        job = current(job_id)
        if job['status'] in {'Fail', 'Unknown', 'Cancelled'} or job['suspension']['phase'] == 'Unverified':
            raise AssertionError(json.dumps(job, indent=2))
        return job if job['suspension']['phase'] == expected else None

    def launch(directory, name):
        directory.mkdir()
        counter = directory/'counter.json'
        program = '''import json,os,sys,time
from pathlib import Path
root=Path(sys.argv[1]); sequence=0; sentinel=5729183
while not (root/'finish').exists():
 sequence+=1
 temporary=root/'counter.tmp'
 temporary.write_text(json.dumps(dict(pid=os.getpid(),sequence=sequence,sentinel=sentinel)))
 temporary.replace(root/'counter.json')
 sum(range(500)); time.sleep(.03)
print('benign counter completed with original in-memory sentinel',sentinel)
'''
        job = command([*cli, 'add', '--name', name, '--cwd', str(directory), '--cpus', '1',
                       '--memory-gib', '.1', '--suspension-backend', 'local-command-v1', '--json', '--',
                       sys.executable, '-m', 'cmw.jobs.suspension_protocol', '--execution-budget', '35',
                       '--hard-timeout', '45', '--', sys.executable, '-c', program, str(directory)])
        if not store.snapshot()['controller']['online']:
            runtime.start(store)
        wait_for(counter.exists)
        wait_for(lambda: ready(job['id']))
        job = current(job['id'])
        owners.extend([job['worker'], job['group'], job['suspension']['capability']['runner'],
                       *job['suspension']['evidence']['computational']])
        return job, counter

    def observe_stopped(job, counter):
        evidence = wait_for(lambda: phase(job['id'], 'Suspended'))
        snapshot = json.loads(counter.read_text())
        assert all(psutil.Process(item['pid']).status() == psutil.STATUS_STOPPED
                   for item in evidence['suspension']['evidence']['computational'])
        time.sleep(.15)
        assert json.loads(counter.read_text()) == snapshot
        assert evidence['status'] == 'Run'
        assert evidence['resources'] == job['resources']
        assert evidence['attempt_id'] == job['attempt_id']
        transitions.append({'action': 'suspend', 'attempt_id': job['attempt_id'],
                            'computational': evidence['suspension']['evidence']['computational'],
                            'sequence': snapshot['sequence'], 'os_stopped': True})
        return snapshot

    async def portal(job, action, size):
        from cmw.jobs.suspension_ui import SuspensionConfirm
        from cmw.jobs.tui import JobsApp
        from textual.widgets import Button, Input
        async def until(predicate, seconds=10):
            limit = time.monotonic() + seconds
            while time.monotonic() < limit:
                if predicate():
                    return
                await asyncio.sleep(.05)
            raise AssertionError('Installed portal transition timed out')
        with patch.object(JobsApp, 'collect_activity'):
            app = JobsApp(store)
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                assert str(app.query_one('#suspension-action', Button).label) == '[U] ' + action.title() + ' job'
                assert app.query_one('#suspension-action').region.bottom <= size[1]
                await pilot.press('u')
                await until(lambda: isinstance(app.screen, SuspensionConfirm))
                modal = app.screen
                assert modal.request['attempt_id'] == job['attempt_id']
                assert modal.request['action'] == action
                await pilot.press('u', 'c', 'p', 'x')
                assert modal.query_one(Input).value == 'ucpx'
                if size == (77, 24):
                    await pilot.resize_terminal(80, 24)
                    await pilot.pause()
                await pilot.click('#suspension-confirm')
                await until(lambda: not isinstance(app.screen, SuspensionConfirm))
                expected = 'Suspended' if action == 'suspend' else 'Run'
                await until(lambda: phase(job['id'], expected) is not None)
                await pilot.press('q')
        assert current(job['id'])['suspension']['phase'] == expected

    with install():
        try:
            missing_cli = [*cli[:-1], str(root/'missing')]
            refused = command([*missing_cli, 'suspend', 'J1.1', '--preview', '--json'], expected=2)
            assert refused['code'] == 'JOBS_ERROR' and not (root/'missing').exists()
            legacy = Store(root/'legacy-state')
            inert = legacy.add(argv=['/unexecuted/legacy'], cwd=root, name='Legacy held fixture', hold=True)
            legacy_cli = [*cli[:-1], str(legacy.root)]
            readonly_before = legacy.path.read_bytes()
            preview = command([*legacy_cli, 'suspend', inert['display_id'], '--preview', '--json'])
            assert preview['supported'] is False and preview['ready'] is False
            assert legacy.path.read_bytes() == readonly_before
            command([*legacy_cli, 'suspend', inert['display_id'], '--json'], expected=2)

            job, counter = launch(root/'completion', 'Installed same-process suspension')
            original = identity(json.loads(counter.read_text())['pid'])
            response = command([*cli, 'suspend', job['display_id'], '--reason', 'installed CLI meeting', '--json'])
            assert response['result'] == 'ACCEPTED'
            before = observe_stopped(job, counter)
            duplicate = command([*cli, 'suspend', job['display_id'], '--json'])
            assert duplicate['result'] == 'COMPLETED'
            assert command([*cli, 'continue', job['display_id'], '--preview', '--json'])['ready']
            assert command([*cli, 'continue', job['display_id'], '--json'])['result'] == 'ACCEPTED'
            wait_for(lambda: phase(job['id'], 'Run'))
            wait_for(lambda: json.loads(counter.read_text())['sequence'] > before['sequence'])
            assert identity(original['pid']) == original
            assert json.loads(counter.read_text())['sentinel'] == 5729183
            if console:
                asyncio.run(portal(job, 'suspend', (77, 24)))
                before = observe_stopped(job, counter)
                assert current(job['id'])['suspension']['phase'] == 'Suspended'
                asyncio.run(portal(job, 'continue', (140, 40)))
                wait_for(lambda: json.loads(counter.read_text())['sequence'] > before['sequence'])
                assert identity(original['pid']) == original
            (counter.parent/'finish').touch()
            wait_for(lambda: current(job['id'])['status'] == 'Done')
            finished = current(job['id'])
            receipt = root/'state'/'attempts'/job['attempt_id']/'payload-exit.json'
            assert json.loads(receipt.read_text())['exit_code'] == 0
            assert finished['attempt_id'] == job['attempt_id'] and finished['exit_code'] == 0
            assert len(finished['suspension']['intervals']) == (2 if console else 1)

            cancelled, cancel_counter = launch(root/'cancellation', 'Installed suspended cancellation')
            command([*cli, 'suspend', cancelled['display_id'], '--json'])
            observe_stopped(cancelled, cancel_counter)
            command([*cli, 'cancel', cancelled['display_id'], '--yes', '--json'])
            wait_for(lambda: current(cancelled['id'])['status'] == 'Cancelled')
            command([*cli, 'continue', cancelled['display_id'], '--json'], expected=2)
            assert current(cancelled['id'])['attempt_id'] == cancelled['attempt_id']
        except BaseException as exc:
            outcome = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            capture(store, root/'transition-evidence', outcome=outcome, started_at=started)
            try:
                for item in store.snapshot()['jobs']:
                    if item['status'] in {'Run', 'Starting'}:
                        store.change(item['id'], 'cancel', confirm=True)
                wait_for(lambda: not any(item['status'] in ACTIVE for item in store.snapshot()['jobs']))
            finally:
                runtime.stop(store)
                wait_for(lambda: not store.snapshot()['controller']['online'])
                runtime.reap_detached()
            wait_for(lambda: not any(owner_alive(item) for item in owners))
    result = {'qualification': 'benign local serial process; no scientific engine or physical sleep',
              'cli_preview_legacy_refusal': 'PASS', 'cli_suspend_continue_duplicate_cancel': 'PASS',
              'original_attempt_pid_memory_and_receipt': 'PASS',
              'portal_live_confirmation_detach_reopen': 'PASS' if console else 'optional console absent',
              'source_origin': str(Path(suspension.__file__).resolve()),
              'transitions': transitions, 'recorded_owners': owners,
              'live_recorded_owners_after_cleanup': sum(owner_alive(item) for item in owners)}
    (root/'suspension-result.json').write_text(json.dumps(result, indent=2)+'\n')
    return result
