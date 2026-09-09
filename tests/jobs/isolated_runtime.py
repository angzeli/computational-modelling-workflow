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
    from cmw.jobs import activity, runtime
    patches = ExitStack()
    patches.enter_context(patch.object(activity, 'DEFAULT_OBSERVER', activity.Observer(collector=fixture_collect)))
    patches.enter_context(patch.object(runtime, 'detached', detached))
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
