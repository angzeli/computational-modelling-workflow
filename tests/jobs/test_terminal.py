"""Actual controlling-terminal tests; no Textual test driver is used here."""
import importlib.util
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shlex
import signal
import struct
import sys
import tempfile
import termios
import time
import unittest

from cmw.jobs.ownership import owner_alive
from cmw.jobs.runtime import reap_detached, stop
from cmw.jobs.store import Store
from tests.jobs.test_jobs import wait_for

ROOT = Path(__file__).resolve().parents[2]


def terminal(argv, env):
    pid, master = pty.fork()
    if pid == 0:
        os.execve(argv[0], argv, env)
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 36, 120, 0, 0))
    return pid, master


def read_until(master, needle, timeout=8):
    output = b''
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        ready,_,_=select.select([master],[],[],.1)
        if ready:
            try:
                data=os.read(master,65536)
            except OSError:
                break
            output+=data
            if needle in output:
                return output
    raise AssertionError(f'Terminal did not render {needle!r}: {output[-1500:]!r}')


def close_terminal(pid, master):
    os.close(master)
    # Closing the controlling terminal should hang up only its own client.
    deadline=time.monotonic()+4
    while time.monotonic()<deadline:
        try:
            ended,_=os.waitpid(pid,os.WNOHANG)
        except ChildProcessError:
            return
        if ended:
            return
        time.sleep(.05)
    os.kill(pid,signal.SIGTERM)
    os.waitpid(pid,0)


@unittest.skipUnless(importlib.util.find_spec('textual'), 'Console terminal test requires the optional CMW jobs extra')
class TerminalTests(unittest.TestCase):
    def test_real_cli_closed_start_terminal_and_watch_detaches(self):
        with tempfile.TemporaryDirectory(prefix='cmw terminal 空格 ') as temporary:
            root=Path(temporary).resolve()
            store=Store(root/'state')
            env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1','TERM':'xterm-256color',
                 'CMW_JOBS_STATE':str(store.root)}
            cli=[sys.executable,'-m','cmw.cli','jobs']
            import subprocess
            release = root/'release-A'
            for name in ('A','B','C'):
                code = (f"import time; from pathlib import Path; p=Path({str(release)!r})\nwhile not p.exists(): time.sleep(.05)" if name=='A' else "import time; print('synthetic'); time.sleep(.15)")
                subprocess.run([*cli,'add','--name',name,'--cwd',str(root),'--cpus','1','--',sys.executable,
                                '-c',code],env=env,check=True,capture_output=True)
            shell,master=terminal(['/bin/bash','--noprofile','--norc','-i'],env)
            try:
                os.write(master,(shlex.join([*cli,'start'])+'\n').encode())
                read_until(master,b'Controller: Online')
                wait_for(lambda: store.snapshot()['jobs'][0]['status']=='Run')
            finally:
                close_terminal(shell,master)
            self.assertTrue(store.snapshot()['controller']['online'])
            try:
                # Real App.run and terminal driver: keyboard details/logs/back/Q.
                for exit_key in (b'q',b'\x03',None):
                    pid,fd=terminal([*cli,'watch'],env)
                    try:
                        rendered=read_until(fd,b'CMW')
                        os.write(fd,b'\r')
                        rendered+=read_until(fd,b'ATTEMPT DETAILS')
                        os.write(fd,b'\x1b')
                        time.sleep(.15)
                        os.write(fd,b'l')
                        rendered+=read_until(fd,b'LOG TAIL')
                        os.write(fd,b'\x1b')
                        time.sleep(.15)
                        if exit_key is not None:
                            os.write(fd,exit_key)
                            deadline = time.monotonic() + 8
                            while time.monotonic() < deadline:
                                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                                    break
                                ready, _, _ = select.select([fd], [], [], .05)
                                if ready:
                                    try:
                                        rendered += os.read(fd, 65536)
                                    except OSError:
                                        pass
                            else:
                                self.fail(f'TUI did not detach with {exit_key!r}')
                        if os.environ.get('CMW_JOBS_TERMINAL_EVIDENCE'):
                            Path(os.environ['CMW_JOBS_TERMINAL_EVIDENCE']).write_bytes(rendered)
                    finally:
                        close_terminal(pid,fd)
                    self.assertTrue(store.snapshot()['controller']['online'])
                    self.assertEqual(store.snapshot()['jobs'][0]['status'], 'Run')
                release.touch()
                wait_for(lambda: all(j['status']=='Done' for j in store.snapshot()['jobs']))
                jobs=store.snapshot()['jobs']
                for a,b in zip(jobs,jobs[1:]):
                    self.assertLessEqual(a['finished_at'],b['started_at'])
            finally:
                for job in store.snapshot()['jobs']:
                    if job['status'] in {'Run','Starting'}:
                        store.change(job['id'],'cancel',confirm=True)
                wait_for(lambda: not any(j['status'] in {'Run','Starting','Cancelling'} for j in store.snapshot()['jobs']))
                stop(store)
                wait_for(lambda: not store.snapshot()['controller']['online'])
                reap_detached()
