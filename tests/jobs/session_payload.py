"""Benign owned-session fixture: reap a subgroup leader while its sibling survives."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from cmw.core.provenance import atomic_write_json
from cmw.jobs.ownership import identity


def main():
    role, directory, mode = sys.argv[1:]
    root = Path(directory)
    release = root/'release'
    if role == 'survivor':
        stopping = False
        def term(signum, frame):
            nonlocal stopping
            (root/'term-seen').touch()
            stopping = mode == 'cooperative'
        signal.signal(signal.SIGTERM, term)
        atomic_write_json(root/'survivor-ready.json',
                          {'identity': identity(), 'sid': os.getsid(0), 'pgid': os.getpgid(0)})
        deadline = time.monotonic()+30
        while not stopping and not release.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        return

    signal.signal(signal.SIGTERM, lambda signum, frame: None)
    # This fixture coordinator is single-threaded; use the Python 3.10 POSIX API.
    leader = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.readline()'],
                              stdin=subprocess.PIPE, preexec_fn=os.setpgrp)
    leader_owner = identity(leader.pid)
    survivor = None
    try:
        survivor = subprocess.Popen([sys.executable, '-m', __spec__.name, 'survivor', directory, mode],
                                    preexec_fn=lambda: os.setpgid(0, leader.pid))
        deadline = time.monotonic()+5
        while not (root/'survivor-ready.json').exists():
            if release.exists() or time.monotonic() >= deadline:
                raise RuntimeError('Fixture survivor did not become ready')
            time.sleep(.02)
        leader.stdin.close()
        leader.wait(timeout=3)
        atomic_write_json(root/'topology.json', {'coordinator': identity(), 'leader': leader_owner,
                          'leader_returncode': leader.returncode,
                          'survivor': json.loads((root/'survivor-ready.json').read_text())})
        survivor.wait(timeout=35)
    finally:
        release.touch()
        if leader.stdin and not leader.stdin.closed:
            leader.stdin.close()
        for child in (leader, survivor):
            if child is None:
                continue
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)


if __name__ == '__main__':
    main()
