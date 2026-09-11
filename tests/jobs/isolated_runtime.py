"""Test-only child entrypoint: activity discovery is limited to owned fixtures.

Production never reads fixture-processes.json or selects this launcher. Tests
propagate the replacement launcher to their own controller and worker children.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def environment():
    return {**os.environ, 'PYTHONPATH': os.pathsep.join((str(ROOT), str(ROOT / 'src'))),
            'PYTHONDONTWRITEBYTECODE': '1'}


def fixture_collect(store, state, deadline):
    from cmw.jobs import activity
    from cmw.jobs.ownership import identity
    import psutil

    fixtures = store.root / 'fixture-processes.json'
    records = json.loads(fixtures.read_text()) if fixtures.exists() else []
    pids = []
    for record in records:
        try:
            if identity(record['pid']) == record:
                pids.append(record['pid'])
        except psutil.NoSuchProcess:
            pass
    return activity.collect(store, state, deadline, pids_provider=lambda: pids)


class FixtureAdmissionSampler:
    """Deterministic admission evidence, enabled only by this test launcher.

The optional fixture file supplies source-specific CPU/available-memory values
for final-admission races. Production never reads it or selects this class.
"""
    def collect(self, store, *, source='controller', wait=False):
        import time
        from cmw.jobs import activity
        state = store.snapshot()
        state['_telemetry_membership'] = True
        guard = activity.DEFAULT_OBSERVER.scan(store, state, source=source)
        guard.pop('_pids', None)
        path = store.root / 'sharing-admission.json'
        config = json.loads(path.read_text()) if path.exists() else {}
        override = config.get(source, {})
        now = time.time()
        samples = []
        for observed in (now-1, now):
            samples.append({'observed_at': observed, 'logical_cpus': 64,
                'machine_usage': {'observed_at': observed, 'stale': False,
                    'cpu_quality': 'fresh', 'memory_quality': 'fresh',
                    'cpu_percent': override.get('cpu_percent', 0),
                    'ram_total_bytes': 64*2**30,
                    'ram_used_bytes': 64*2**30-override.get('available_gib', 63)*2**30},
                'jobs': {}, 'external': {}})
        return guard, {'samples': samples, 'source': source}


def detached(arguments, log):
    from cmw.jobs import runtime
    log.parent.mkdir(parents=True, exist_ok=True)
    runtime.reap_detached()
    with log.open('ab', buffering=0) as output:
        process = subprocess.Popen(
            [sys.executable, '-m', 'tests.jobs.isolated_runtime', *arguments],
            stdin=subprocess.DEVNULL, stdout=output, stderr=output,
            start_new_session=True, close_fds=True, env=environment(), cwd='/')
    runtime._CHILDREN.append(process)
    return process


def install():
    from cmw.jobs import activity, runtime, sharing
    patches = ExitStack()
    patches.enter_context(patch.object(activity, 'DEFAULT_OBSERVER', activity.Observer(collector=fixture_collect)))
    patches.enter_context(patch.object(runtime, 'detached', detached))
    patches.enter_context(patch.object(sharing, 'AdmissionSampler', FixtureAdmissionSampler))
    return patches


def main():
    with install():
        if sys.argv[1] == 'cli':
            from cmw.cli import main as cli_main
            del sys.argv[1]
            raise SystemExit(cli_main())
        from cmw.jobs.runtime import main as runtime_main
        runtime_main()


if __name__ == '__main__':
    main()
