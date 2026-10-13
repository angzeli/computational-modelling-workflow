"""Runner finalization uses strict observed identities, never apparent emptiness."""
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import psutil

from cmw.jobs import ownership
from cmw.jobs.suspension_protocol import RunnerControl, SuspensionProtocolError


def owner(pid):
    return {'pid': pid, 'birth': float(pid), 'host': 'fixture-host', 'boot': 1.0}


class RunnerDrainageTests(unittest.TestCase):
    def setUp(self):
        self.anchor, self.runner, self.root = owner(100), owner(200), owner(300)
        self.state = 'running'
        self.members = [100, 200, 300]
        self.group_visible = False
        self.deny_probe = False
        self.control = RunnerControl.__new__(RunnerControl)
        self.control.context = {'anchor': self.anchor, 'backend': 'fictional-v1'}
        self.control.runner = self.runner
        self.control.binding = {}
        self.control.deadline = {}
        self.control.storage = []
        self.control.runtime = {}
        self.control.clock = {}
        self.control._registered = False
        self.control._state = 'run'
        self.control._last_command = None
        self.control._last_ack_clock = None
        self.control.intervals = []
        self.control.last_error = ''
        self.control.accounting_uncertain = False
        self.control._check_directory = lambda: None
        self.control._check_authority = lambda **kwargs: None
        self.control._check_storage = lambda: None
        self.control._read = lambda name, **kwargs: self.control.context if name == 'context.json' else None
        self.control._write = lambda *args: None
        stack = self.enterContext(ExitStack())
        for target, options in (
            ('cmw.jobs.suspension_protocol.os.getpid', {'return_value': 200}),
            ('cmw.jobs.suspension_protocol.os.getsid', {'side_effect': self.sid}),
            ('cmw.jobs.suspension_protocol.os.getpgid', {'side_effect': lambda pid: 300 if pid == 300 else 100}),
            ('cmw.jobs.suspension_protocol.os.killpg', {'side_effect': self.probe}),
            ('cmw.jobs.suspension_protocol.psutil.pids', {'side_effect': lambda: list(self.members)}),
            ('cmw.jobs.suspension_protocol.psutil.Process', {'side_effect': self.process}),
            ('cmw.jobs.suspension_protocol.identity', {'side_effect': self.identity}),
            ('cmw.jobs.ownership.identity', {'side_effect': self.identity}),
            ('cmw.jobs.suspension_protocol.owner_alive', {'return_value': True}),
            ('cmw.jobs.suspension_protocol.now_clock', {'return_value': 1.0}),
        ):
            stack.enter_context(patch(target, **options))
        # Preserve the baseline enumerator's actual zombie/ESRCH behavior while
        # supplying a fully deterministic process model rather than host PIDs.
        stack.enter_context(patch('cmw.jobs.suspension_protocol.group_members', create=True,
                                  side_effect=lambda sid, **kw: ownership.group_members(
                                      sid, pids=self.members, process_factory=self.process, **kw)))
        self.control.register(300)
        self.control.poll()

    def identity(self, pid=None):
        pid = 200 if pid is None else pid
        if pid == 300 and self.state == 'absent':
            raise psutil.NoSuchProcess(pid)
        return {**owner(pid), **({'birth': 301.0} if pid == 300 and self.state == 'reused' else {})}

    def sid(self, pid):
        if pid == 300 and self.state == 'sid-esrch':
            raise ProcessLookupError(3, 'exiting fixture')
        return 100

    def process(self, pid=None):
        pid = 200 if pid is None else pid
        self.identity(pid)
        return SimpleNamespace(pid=pid, create_time=lambda: self.identity(pid)['birth'],
                               status=lambda: 'zombie' if pid == 300 and self.state == 'zombie' else 'sleeping',
                               ppid=lambda: 100 if pid == 200 else 200 if pid == 300 else 0,
                               parents=lambda: [self.process(100)] if pid == 200 else [])

    def probe(self, pgid, signum):
        self.assertEqual(signum, 0, 'Finalization must not send a nonzero signal')
        self.assertEqual(pgid, 300)
        if self.deny_probe:
            raise PermissionError(1, 'fixture denied')
        if self.group_visible:
            return
        raise ProcessLookupError(3, 'fixture group absent')

    def test_same_birth_sid_esrch_does_not_release_finalization(self):
        self.state = 'sid-esrch'
        self.assertFalse(self.control.domain_drained())

    def test_observable_zombie_does_not_release_finalization(self):
        self.state = 'zombie'
        self.assertFalse(self.control.domain_drained())

    def test_genuinely_absent_original_and_group_allow_finalization(self):
        self.state = 'absent'
        self.members.remove(300)
        self.assertTrue(self.control.domain_drained())

    def test_absent_original_with_kernel_visible_group_does_not_finalize(self):
        self.state = 'absent'
        self.members.remove(300)
        self.group_visible = True
        self.assertFalse(self.control.domain_drained())

    def test_known_identity_missing_from_enumeration_still_prevents_finalization(self):
        self.state = 'zombie'
        self.members.remove(300)
        self.assertFalse(self.control.domain_drained())

    def test_denied_zero_probe_does_not_finalize_or_send_termination(self):
        self.state = 'absent'
        self.members.remove(300)
        self.deny_probe = True
        self.assertFalse(self.control.domain_drained())
        self.assertIn('absence cannot be verified', self.control.last_error)
        self.deny_probe = False
        self.assertTrue(self.control.domain_drained())

    def test_authority_loss_after_empty_observation_refuses_finalization(self):
        self.state = 'absent'
        self.members.remove(300)
        with patch.object(self.control, '_check_authority',
                          side_effect=[None, SuspensionProtocolError('Worker authority lost')]):
            self.assertFalse(self.control.domain_drained())
        self.assertIn('authority lost', self.control.last_error)

    def test_late_owned_descendant_prevents_root_exit_finalization(self):
        self.members.append(400)
        self.control.poll()
        self.state = 'absent'
        self.members.remove(300)
        self.assertFalse(self.control.domain_drained())

    def test_reused_original_identity_does_not_finalize(self):
        self.state = 'reused'
        self.assertFalse(self.control.domain_drained())


if __name__ == '__main__':
    unittest.main()
