"""Stream a module's regular-file output only until its direct interpreter exits.

A fixture descendant may inherit stdout/stderr and survive a native timeout.
It must not keep the CI reporting pipe open. Its late output remains in
log.live; log is the bounded snapshot consumed by summaries and failure reports.
This helper observes only its direct child and never signals fixture processes.
"""
import os
from pathlib import Path
import subprocess
import sys
import time


def run(log_path, module):
    log_path = Path(log_path)
    live_path = log_path.with_name(log_path.name + '.live')
    command = [sys.executable, '-c',
        'import sys; sys.path.insert(0, "tests"); '
        'exec(compile(open("scripts/ci/unittest_guard.py", "rb").read(), '
        '"scripts/ci/unittest_guard.py", "exec"))', module]
    with live_path.open('xb', buffering=0) as destination, live_path.open('rb', buffering=0) as source, \
            log_path.open('xb', buffering=0) as snapshot:
        process = subprocess.Popen(command, stdout=destination, stderr=subprocess.STDOUT)
        while True:
            status = process.poll()
            # Capture one finite boundary. Even a surviving writer producing
            # output continuously cannot extend this drain after direct exit.
            end = os.fstat(source.fileno()).st_size
            while source.tell() < end:
                data = source.read(min(65536, end - source.tell()))
                if not data:
                    break
                snapshot.write(data)
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            if status is not None:
                return status if status >= 0 else 128 - status
            time.sleep(.01)


if __name__ == '__main__':
    raise SystemExit(run(sys.argv[1], sys.argv[2]))
