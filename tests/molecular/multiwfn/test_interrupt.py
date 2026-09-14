"""Standalone interruption keeps fake Multiwfn resources alive until termination."""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import psutil

from cmw.jobs import runtime
from cmw.jobs.ownership import identity, owner_alive
from cmw.jobs.store import Store
from tests.jobs.isolated_runtime import environment, install
from tests.jobs.lifecycle_evidence import bounded_tail
from tests.jobs.test_jobs import wait_for

ROOT = Path(__file__).resolve().parents[3]
SHELL = ROOT/'scripts/multiwfn/multiwfn_runtime.sh'


def interrupt_owner_observation(owner):
    if owner is None:
        return {'recorded': None, 'verdict': 'missing'}
    result = {'recorded': owner}
    try:
        current = identity(owner['pid'])
        if current != owner:
            return {**result, 'verdict': 'mismatch'}
        process = psutil.Process(owner['pid'])
        status = process.status()
        if identity(owner['pid']) != owner:
            return {**result, 'verdict': 'changed-during-observation'}
        return {**result, 'verdict': 'matched', 'status': status}
    except psutil.NoSuchProcess:
        return {**result, 'verdict': 'gone'}
    except Exception as exc:
        return {**result, 'verdict': 'unavailable', 'error': type(exc).__name__}


def capture_interrupt_failure(root, *, test_id, failure, started, parent, parent_owner, child, ready, stderr_path):
    """Observe only this fixture before release; diagnostics cannot replace its error."""
    try:
        if ready is None and (root/'ready.json').is_file():
            ready = json.loads(bounded_tail(root/'ready.json', 4096))
        child = child or (ready or {}).get('owner')
        data = {
            'test': test_id, 'failure': type(failure).__name__,
            'elapsed_seconds': time.monotonic()-started,
            'parent_returncode': parent.poll() if parent else None,
            'parent': interrupt_owner_observation(parent_owner),
            'child': interrupt_owner_observation(child),
            'markers': {name: (root/name).exists() for name in
                        ('ready.json', 'signal-seen', 'release', 'alias-lost-while-live')},
            'ready': ready,
            'aliases': [{'path': path, 'exists': Path(path).exists(),
                         'is_symlink': Path(path).is_symlink()}
                        for path in (ready or {}).get('aliases', [])[:2]],
        }
        try:
            data['stderr_tail'] = bounded_tail(stderr_path, 16384)
        except OSError as exc:
            data['stderr_error'] = type(exc).__name__
        encoded = json.dumps(data, ensure_ascii=True)
        try:
            print('CMW_INTERRUPT_FAILURE '+encoded, file=sys.stderr, flush=True)
        except Exception:
            pass
        evidence_root = os.environ.get('CMW_TEST_EVIDENCE_DIR')
        if evidence_root:
            Path(evidence_root).mkdir(parents=True, exist_ok=True, mode=0o700)
            destination = Path(tempfile.mkdtemp(prefix='multiwfn-interrupt-', dir=evidence_root))
            (destination/'capture.json').write_text(encoded+'\n')
    except Exception:
        pass


class InterruptTests(unittest.TestCase):
    def exercise(self, signum=signal.SIGTERM, *, queued=False, unresolved=False, early_interrupt=False):
        with tempfile.TemporaryDirectory(prefix='cmw-interrupt-') as directory, install():
            root = Path(directory)
            attempt = root/'attempt'
            attempt.mkdir()
            (root/'tmp').mkdir()
            (root/'settings.ini').write_text('nthreads= 1\n')
            (root/'source.xyz').write_text('1\nsynthetic\nH 0 0 0\n')
            engine = root/'engine.py'
            engine.write_text('''import os,signal,sys,time
from pathlib import Path
from cmw.core.provenance import atomic_write_json
from cmw.jobs.ownership import identity
root=Path(os.environ['CMW_TEST_ROOT'])
def interrupted(signum, frame):
    (root/'signal-seen').touch()
signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)
aliases=[Path(os.environ['Multiwfnpath']),Path(sys.argv[1])]
atomic_write_json(root/'ready.json', {'owner':identity(),'aliases':[str(p) for p in aliases]})
deadline=time.monotonic()+15
while not (root/'release').exists() and time.monotonic()<deadline:
    if not all(p.exists() for p in aliases): (root/'alias-lost-while-live').touch()
    time.sleep(.02)
''')
            executable = root/'fake-multiwfn'
            executable.write_text('#!/bin/bash\nexec '+shlex.join([sys.executable, str(engine)])+' "$@"\n')
            executable.chmod(0o700)
            command = ['/bin/bash', '-c',
                'set -euo pipefail; '
                # Keep expansion until each traced command; no timestamp subprocesses.
                '''PS4='+ seconds=${SECONDS} line=${LINENO}: '; set -x; '''
                'source "$1"; MULTIWFN_EXE="$2"; MULTIWFN_NTHREADS=1; '
                'MULTIWFN_RUN_SETTINGS_PATH="$3"; MULTIWFN_RUN_SETTINGS_DIRECTORY=${3%/settings.ini}; '
                'if [[ ${CMW_TEST_REFUSE_KILL:-0} == 1 ]]; then '
                'kill() { if [[ "$1" == -KILL ]]; then return 1; fi; builtin kill "$@"; }; fi; '
                # Deliver TERM after $! is captured but before the first wait/poll.
                'if [[ ${CMW_TEST_EARLY_INTERRUPT:-0} == 1 ]]; then set -T; '
                '''trap 'if [[ -n ${child_pid:-} && ${CMW_TEST_EARLY_INTERRUPT:-0} == 1 ]]; then '''
                '''CMW_TEST_EARLY_INTERRUPT=0; trap - DEBUG; '''
                '''for ready_probe in {1..250}; do '''
                '''[[ ! -f $CMW_TEST_ROOT/ready.json && ! -f $CMW_TEST_ROOT/release ]] || break; '''
                '''sleep 0.02; done; [[ -f $CMW_TEST_ROOT/ready.json ]] || exit 75; '''
                '''kill -TERM $$; fi' DEBUG; fi; '''
                'multiwfn_runtime_launch "$4"', '_', str(SHELL), str(executable),
                str(root/'settings.ini'), str(root/'source.xyz')]
            env = {**environment(), 'CMW_TEST_ROOT': str(root), 'TMPDIR': str(root/'tmp')}
            if unresolved:
                env['CMW_TEST_REFUSE_KILL'] = '1'
            if early_interrupt:
                env['CMW_TEST_EARLY_INTERRUPT'] = '1'
            sentinel = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])
            sentinel_owner = identity(sentinel.pid)
            parent = None
            parent_owner = None
            store = Store(root/'state') if queued else None
            child = None
            ready = None
            stderr_path = root/'wrapper.stderr'
            started = time.monotonic()
            try:
                if queued:
                    job = store.add(argv=command, cwd=attempt, name='fake Multiwfn interruption',
                                    env={'CMW_TEST_ROOT': str(root), 'TMPDIR': str(root/'tmp')})
                    stderr_path = Path(job['logs']['stderr'])
                    runtime.start(store)
                else:
                    with (root/'wrapper.stderr').open('w') as stderr_file:
                        parent = subprocess.Popen(command, cwd=attempt, env=env, stdout=subprocess.DEVNULL,
                                                  stderr=stderr_file)
                    parent_owner = identity(parent.pid)
                wait_for((root/'ready.json').exists, timeout=5)
                ready = json.loads((root/'ready.json').read_text())
                child = ready['owner']
                if queued:
                    store.change(job['id'], 'cancel', confirm=True)
                elif not early_interrupt:
                    parent.send_signal(signum)
                wait_for((root/'signal-seen').exists, timeout=2)
                self.assertTrue(owner_alive(child))
                self.assertTrue(all(Path(p).exists() for p in ready['aliases']))
                self.assertFalse((root/'alias-lost-while-live').exists())
                if queued:
                    self.assertEqual(store.snapshot()['jobs'][0]['status'], 'Cancelling')
                    wait_for(lambda: store.snapshot()['jobs'][0]['status'] == 'Cancelled', timeout=8)
                else:
                    self.assertIsNone(parent.poll())
                    # Unresolved termination traverses both bounded polling loops;
                    # allow process-launch overhead beyond their four seconds of sleep.
                    parent.wait(timeout=10 if unresolved else 6)
                    stderr = (root/'wrapper.stderr').read_text()
                    if unresolved:
                        self.assertEqual(parent.returncode, 75, stderr)
                        self.assertIn('termination unresolved; aliases retained', stderr)
                        self.assertTrue(all(Path(p).exists() for p in ready['aliases']))
                    else:
                        self.assertEqual(parent.returncode, 128+signum, stderr)
                        self.assertTrue(all(not Path(p).exists() for p in ready['aliases']))
                self.assertEqual(owner_alive(child), unresolved)
                self.assertFalse((root/'alias-lost-while-live').exists())
                self.assertEqual(identity(sentinel.pid), sentinel_owner)
                self.assertIsNone(sentinel.poll())
            except BaseException as exc:
                capture_interrupt_failure(root, test_id=self.id(), failure=exc, started=started,
                    parent=parent, parent_owner=parent_owner, child=child, ready=ready,
                    stderr_path=stderr_path)
                raise
            finally:
                (root/'release').touch()
                if parent:
                    parent.wait(timeout=6)
                if store and store.path.exists():
                    controller = store.snapshot()['controller'].get('owner')
                    runtime.stop(store)
                    wait_for(lambda: not store.snapshot()['controller']['online'])
                    for process in runtime._CHILDREN:
                        if controller and process.pid == controller['pid']:
                            process.wait(timeout=5)
                    runtime.reap_detached()
                wait_for(lambda: child is None or not owner_alive(child), timeout=5)
                sentinel.terminate()
                sentinel.wait(timeout=5)

    def test_term_resistant_direct_child_keeps_aliases_until_ended(self):
        self.exercise()

    def test_int_resistant_direct_child_preserves_interrupt_status(self):
        self.exercise(signal.SIGINT)

    def test_jobs_wrapped_child_ends_before_terminal_state(self):
        self.exercise(queued=True)

    def test_unresolved_termination_retains_aliases_and_reports_uncertainty(self):
        self.exercise(unresolved=True)

    def test_interrupt_before_wait_still_reaches_bounded_shutdown(self):
        self.exercise(early_interrupt=True)
