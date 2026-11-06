"""A transient observation never erases positive evidence against closure."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cmw.jobs.successor import _closure


class ClosureVerdictTests(unittest.TestCase):
    def verdict(self, checks, *, lock='unheld', members=()):
        job = {'id': 1, 'attempt_id': 'a' * 32, 'cwd': '/unused', 'status': 'Done',
               'worker_protocol': 3, 'worker': {'pid': 1}, 'group': {'pid': 2}}
        with (patch('cmw.periodic.vasp.result_execution.inspect_execution_snapshot',
                    return_value={'checks': checks, 'live_queue': {
                        'status': 'UNAVAILABLE', 'reason': 'database is locked'}}),
              patch('cmw.jobs.successor.observe_process', return_value=SimpleNamespace(verdict='gone')),
              patch('cmw.jobs.successor.observe_lock', return_value=SimpleNamespace(verdict=lock)),
              patch('cmw.jobs.ownership.group_members', return_value=members)):
            return _closure(SimpleNamespace(root=Path('/unused')), job, {'schema': 4},
                            {'node_id': 'node', 'queue_id': 'queue'})

    def test_only_unavailable_evidence_keeps_reason_and_null(self):
        result = self.verdict({'native_queue': None, 'jobs_receipt': None})
        self.assertIsNone(result['closed'])
        self.assertIn('parent queue temporarily unreadable', result['reason'])
        self.assertIn('jobs_receipt', result['reason'])
        self.assertIsNone(result['checks']['native_queue'])

    def test_independent_contradictions_win_over_an_unreadable_queue(self):
        for checks, lock, members, reason in (
                ({'receipt_schema': False}, 'unheld', (), 'receipt_schema'),
                ({}, 'held', (), 'lock'),
                ({}, 'unheld', (17,), 'live members')):
            with self.subTest(reason=reason):
                result = self.verdict({'native_queue': None, **checks}, lock=lock, members=members)
                self.assertIs(result['closed'], False)
                self.assertIn(reason, result['reason'])
