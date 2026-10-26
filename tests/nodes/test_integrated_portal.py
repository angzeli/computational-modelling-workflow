"""Minimal cross-feature queue faults; no endpoint or calculation processes."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from cmw.nodes.protocol import NodeError
from tests.nodes.test_portal import FakeBackend

try:
    from cmw.nodes.portal import NodesApp
    from cmw.doctor.presentation import DoctorReport
    from textual.widgets import TextArea
except ImportError:
    NodesApp = None


@unittest.skipIf(NodesApp is None, 'Optional Jobs UI is unavailable')
class IntegratedPortalTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_resolves_queued_readers_without_retarget_or_replay(self):
        backend = FakeBackend('OWNER', remote=True)
        app = NodesApp({'OWNER': backend})
        target = deepcopy(backend.jobs[2]['target'])
        deliveries = []
        for operation in ('doctor.inspect', 'review.preview', 'catalogue.show'):
            app.queue('OWNER', operation, target=target,
                      callback=lambda value, error, operation=operation:
                          deliveries.append((operation, value, error.code)))
        error = NodeError('DISCONNECTED', 'fixture stream ended')
        with patch.object(app, 'render_state'):
            app.accept('OWNER', ('progress', {}, target, None), None, error)
        self.assertEqual(sorted(deliveries), sorted([
            (operation, None, 'DISCONNECTED')
            for operation in ('doctor.inspect', 'review.preview', 'catalogue.show')]))
        self.assertFalse(app.lanes['OWNER'].pending)
        self.assertEqual(backend.calls, [])
        self.assertEqual(target, backend.jobs[2]['target'])

    async def test_runtime_pin_change_invalidates_cached_freshness(self):
        backend = FakeBackend('OWNER', remote=True)
        app = NodesApp({'OWNER': backend}, clock=lambda: 100.)
        lane = app.lanes['OWNER']
        lane.received = 100.
        self.assertTrue(lane.fresh(100.))
        refusals = []
        app.queue('OWNER', 'doctor.inspect', target=backend.jobs[0]['target'],
                  callback=lambda value, error: refusals.append(error.code))
        with patch.object(app, 'render_state'):
            app.accept('OWNER', ('doctor.inspect', {}, backend.jobs[0]['target'], None),
                       None, NodeError('RUNTIME_MISMATCH', 'fixture changed runtime'))
        self.assertFalse(lane.fresh(100.), 'Changed runtime left prior owner evidence fresh')
        self.assertEqual(refusals, ['RUNTIME_MISMATCH'])

    async def test_reply_before_doctor_mount_is_delivered_once(self):
        backend = FakeBackend('OWNER', remote=True)
        app = NodesApp({'OWNER': backend})
        target = deepcopy(backend.jobs[0]['target'])
        caller = {'schema': 1, 'runtime': {'identity': {'fingerprint': 'frontend'}},
                  'capabilities': [], 'findings': [], 'observation': {}, 'worker': {'status': 'UNKNOWN'}}
        with patch('cmw.doctor.service.inspect_local', return_value=caller), \
                patch('cmw.doctor.service.format_report', return_value='Captured refusal'):
            async with app.run_test(size=(77, 24)) as pilot:
                await pilot.pause()
                def immediate(owner, operation, params, selected, callback):
                    self.assertEqual(selected, target)
                    callback(None, NodeError('UNSUPPORTED_OPERATION', 'fixture old endpoint'))
                    return True
                with patch.object(app, 'queue', side_effect=immediate):
                    app.open_doctor('OWNER', target, 'OWNER / research / J1.1')
                for _ in range(40):
                    await pilot.pause(.025)
                    if not app.screen.pending:
                        break
                self.assertIsInstance(app.screen, DoctorReport)
                self.assertFalse(app.screen.pending, 'A pre-mount reply was discarded')
                self.assertIn('Captured refusal', app.screen.query_one(TextArea).text)
                self.assertEqual(app.screen.report['target']['attempt_id'], target['attempt_id'])
                await pilot.press('escape')
                self.assertNotIsInstance(app.screen, DoctorReport)
