"""Compact federated UI with typed synthetic evidence and independent fake nodes.

These tests qualify frontend routing/rendering, not physical SSH execution.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs.clipboard import CopyResult
from cmw.jobs.source_selection import SourceInspection, SourceValidation, SessionSelection
from cmw.nodes.codec import encode_typed
from cmw.nodes.protocol import NodeError
from cmw.progress.models import HistoryPage, HistoryEntry, SourceDescriptor
from tests.progress.test_electronic_notation import electronic_example

try:
    from cmw.nodes.portal import NodesApp, row_key, target_key
    from cmw.nodes.portal_views import NodeFiles, FetchDialog, NodeControlConfirm, NodeCopyPreview
    from textual.widgets import DataTable, Input, Static
except ImportError:
    NodesApp = None


class FakeBackend:
    def __init__(self, alias, *, remote=False):
        self.alias, self.queue = alias, 'research'
        self.node_id, self.queue_id = uuid4().hex, uuid4().hex
        self.connection = {'transport': 'ssh' if remote else 'local', 'read_only': False}
        self.calls, self.blocks = [], {}
        self.fail = False
        self.closed = False
        self.sequence, self.session = 0, uuid4().hex
        self.jobs = []
        for index in range(5):
            target = {'node_id': self.node_id, 'queue_id': self.queue_id, 'job_id': index + 1, 'attempt_id': uuid4().hex}
            self.jobs.append({'id': index + 1, 'display_id': f'J{index + 1}.1', 'attempt_id': target['attempt_id'],
                'target': target, 'revision': str(index), 'name': alias + ' synthetic selected computation',
                'engine': 'VASP', 'status': 'Run' if index == 0 else 'Hold', 'elapsed': 60,
                'cwd': '/Volumes/SameSpelled/remote-only', 'resources': {'cpus': 8, 'memory_gib': 8},
                'argv': ['/never/executed'], 'order': index + 1, 'note': ''})
        self.context = replace(electronic_example().context, binding_id=alias, job_id=1,
                               attempt_id=self.jobs[0]['attempt_id'], sources=(SourceDescriptor('OUTCAR', '/remote/' + alias + '/OUTCAR'),))
        self.snapshot = replace(electronic_example(), context=self.context)

    def close(self):
        self.closed = True
        for gate in self.blocks.values():
            gate.set()

    def call(self, operation, params=None, target=None, **kwargs):
        params = params or {}
        self.calls.append((operation, deepcopy(params), deepcopy(target)))
        if operation in self.blocks:
            self.blocks[operation].wait(5)
        if self.fail:
            raise NodeError('UNREACHABLE', 'synthetic unavailable node')
        if target is not None:
            assert target['node_id'] == self.node_id and target['queue_id'] == self.queue_id
        self.sequence += 1
        if operation == 'status':
            result = {'state': {'jobs': deepcopy(self.jobs), 'controller': {'online': True, 'dispatch': True},
                'job_usage': {self.jobs[0]['attempt_id']: {'cpu_cores': 7.91, 'cpu_quality': 'complete',
                    'rss_bytes': 2**30, 'memory_quality': 'complete', 'age_seconds': 0}},
                'external_activity': {'state': 'NO_MATCH'}, 'admission': {'permitted': False}},
                'has_more': False}
        elif operation == 'progress':
            context = replace(self.context, job_id=target['job_id'], attempt_id=target['attempt_id'])
            result = {'snapshot': encode_typed(replace(self.snapshot, context=context))}
        elif operation == 'observe':
            result = {'execution': {'attempt_id': target['attempt_id'], 'job_id': target['job_id'],
                'summary': 'worker + anchor only', 'observed_execution': 'VERIFIED_WORKER', 'observed_at': 112}}
        elif operation == 'history':
            context = replace(self.context, job_id=target['job_id'], attempt_id=target['attempt_id'])
            result = {'page': encode_typed(HistoryPage(context, status='OBSERVED',
                entries=(HistoryEntry('E1', 1, native_label=self.alias + ' evaluation'),), source_revision='source-v1')), 'snapshot': None}
        elif operation == 'history.release':
            result = {'released': True}
        elif operation == 'locations':
            result = {'target': target, 'status': 'AVAILABLE', 'diagnostics': [], 'locations': [{
                **target, 'location_id': 'output', 'node_path': '/Volumes/SameSpelled/remote-only',
                'declared_path': '/Volumes/SameSpelled/remote-only', 'role': 'output', 'kind': 'directory',
                'accessibility': 'AVAILABLE', 'storage_id': 'registered-storage', 'qualification': 'declared',
                'detail': '', 'local_open_permitted': False}]}
        elif operation == 'sources.inspect':
            result = {'inspection': encode_typed(SourceInspection(target_key(target), self.context, (), self.context.sources))}
        elif operation == 'sources.validate':
            result = {'validation_id': 'validate-' + self.alias,
                'validation': encode_typed(SourceValidation(target_key(target), self.context, self.context.sources, ()))}
        elif operation == 'sources.activate':
            result = {'selection': encode_typed(SessionSelection(target_key(target), self.context.sources, (), 1, self.alias))}
        elif operation == 'sources.clear':
            result = {'cleared': True}
        elif operation == 'control.preview':
            result = {'ready': True, 'reasons': [], 'confirmation_required': True, 'node_id': self.node_id,
                'queue_id': self.queue_id, 'target': target, 'action': params['action'], 'expected_clock_id': 'clock-' + self.alias,
                'expected_revision': {'job': 3 if target else None, 'queue': 9}, **({'position': params['position']} if 'position' in params else {})}
        elif operation == 'logs':
            result = {'text': 'bounded stdout of ' + self.alias, 'node_path': '/remote/stdout'}
        else:
            raise AssertionError(operation)
        return {'schema': 1, 'node_id': self.node_id, 'queue_id': self.queue_id, 'queue_alias': self.queue,
            'session_id': self.session, 'sequence': self.sequence, 'observed_at': 112.,
            'clock_id': 'clock-' + self.alias, 'runtime': {'fingerprint': 'runtime-' + self.alias, 'version': 'test'},
            'host': {'platform': 'darwin', 'hardware_id': self.alias}, 'result': result}


@unittest.skipIf(NodesApp is None, 'Jobs UI extra unavailable')
class PortalTests(unittest.IsolatedAsyncioTestCase):
    async def until(self, pilot, predicate):
        for _ in range(80):
            if predicate():
                return
            await pilot.pause(.05)
        self.fail('Frontend condition did not become true')

    def backends(self):
        return {'MBP': FakeBackend('MBP'), 'MINI': FakeBackend('MINI', remote=True)}

    async def test_global_selection_and_framed_compact_rendering_at_required_sizes(self):
        backends = self.backends()
        app = NodesApp(backends, clock=lambda: 1000.)
        async with app.run_test(size=(77, 24)) as pilot:
            await self.until(pilot, lambda: len(app.rows) == 10)
            target = backends['MINI'].jobs[0]['target']
            app.select(row_key(target))
            app.collect_progress()
            await self.until(pilot, lambda: app.selected_snapshot('MINI', backends['MINI'].jobs[0]) is not None)
            for size in ((77, 24), (80, 24), (100, 30), (140, 40)):
                await pilot.resize_terminal(*size)
                app.render_state()
                await pilot.pause()
                table = app.query_one('#table', DataTable)
                card = str(app.query_one('#progress', Static).render())
                self.assertEqual(app.selected_key, row_key(target))
                self.assertGreaterEqual(table.size.height - table.header_height, 3)
                self.assertLessEqual(table.scroll_y, table.cursor_row)
                self.assertLess(table.cursor_row, table.scroll_y + table.size.height - table.header_height)
                self.assertIn('Q Detach', str(app.query_one('#shortcuts', Static).render()))
                self.assertLessEqual(app.query_one('#command-bar').region.bottom, size[1])
                self.assertLessEqual(app.query_one('#progress').region.bottom, app.query_one('#command-bar').region.y)
                self.assertIn('┌─', card)
                for token in ('SCF', 'FORCE', 'ELECTRONIC', 'EDIFF 1E-5 eV', '1.5254e-04 eV @SCF8'):
                    self.assertIn(token, card)
                self.assertIn('MINI / research / J1.1', str(app.query_one('#selected', Static).render()))
                self.assertIn('data 12s ago', card)  # Endpoint 112 - evidence 100, not frontend 1000.

    async def test_slow_node_does_not_block_local_refresh_or_navigation(self):
        backends = self.backends()
        gate = threading.Event()
        backends['MINI'].blocks['status'] = gate
        app = NodesApp(backends)
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                await self.until(pilot, lambda: app.lanes['MBP'].state is not None)
                self.assertIsNone(app.lanes['MINI'].state)
                await pilot.press('down')
                self.assertEqual(app.selected()[1]['display_id'], 'J2.1')
                app.lanes['MBP'].next_poll = 0
                await self.until(pilot, lambda: sum(call[0] == 'status' for call in backends['MBP'].calls) >= 2)
                self.assertIsNone(app.lanes['MINI'].state)
        finally:
            gate.set()

    async def test_unreachable_node_qualifies_cached_run_and_telemetry(self):
        backends = self.backends()
        app = NodesApp(backends)
        async with app.run_test(size=(77, 24)) as pilot:
            await self.until(pilot, lambda: len(app.rows) == 10)
            app.select(row_key(backends['MINI'].jobs[0]['target']))
            backends['MINI'].fail = True
            app.lanes['MINI'].next_poll = 0
            await self.until(pilot, lambda: app.lanes['MINI'].error is not None)
            selected = str(app.query_one('#selected', Static).render())
            self.assertIn('Last reported Run — node unreachable', selected)
            self.assertIn('791%~', selected)
            self.assertTrue(all(len(line) <= 70 for line in selected.splitlines()), selected)
            before = len(backends['MINI'].calls)
            app.action_cancel()
            await pilot.pause(.1)
            self.assertEqual(len(backends['MINI'].calls), before)
            self.assertIn('Nothing was queued', str(app.query_one('#message', Static).render()))

    async def test_delayed_files_result_cannot_follow_selection_to_another_node(self):
        backends = self.backends()
        gate = threading.Event()
        backends['MINI'].blocks['locations'] = gate
        app = NodesApp(backends)
        try:
            async with app.run_test(size=(80, 24)) as pilot:
                await self.until(pilot, lambda: len(app.rows) == 10)
                app.select(row_key(backends['MINI'].jobs[0]['target']))
                app.action_files()
                await self.until(pilot, lambda: any(call[0] == 'locations' for call in backends['MINI'].calls))
                app.select(row_key(backends['MBP'].jobs[0]['target']))
                gate.set()
                await self.until(pilot, lambda: not app.lanes['MINI'].busy)
                self.assertNotIsInstance(app.screen, NodeFiles)
                self.assertEqual(app.selected()[0], 'MBP')
        finally:
            gate.set()

    async def test_files_history_sources_and_copy_keep_full_node_identity(self):
        backends = self.backends()
        copied = []
        app = NodesApp(backends, clipboard_backend=lambda text: (copied.append(text) or CopyResult('LOCAL_ACCEPTED', 'local fixture')))
        async with app.run_test(size=(100, 30)) as pilot:
            await self.until(pilot, lambda: len(app.rows) == 10)
            job = backends['MINI'].jobs[0]
            app.select(row_key(job['target']))
            app.collect_progress()
            await self.until(pilot, lambda: app.selected_snapshot('MINI', job) is not None)
            app.action_files()
            await self.until(pilot, lambda: isinstance(app.screen, NodeFiles))
            self.assertEqual(app.screen.target, job['target'])
            await pilot.pause()
            with patch('cmw.jobs.folder_opener.request_open', side_effect=AssertionError('Remote path must never open locally')):
                await pilot.click('#node-files-open')
                self.assertIn('never opened locally', str(app.screen.query_one('#node-files-status', Static).render()))
            await pilot.click('#node-files-copy')
            await self.until(pilot, lambda: bool(copied))
            self.assertIn('MINI / research / J1.1', copied[0])
            await pilot.press('escape')
            app.action_history()
            await self.until(pilot, lambda: app.history_view is not None and app.history_view.page is not None)
            self.assertEqual(app.history_view.selection, target_key(job['target']))
            self.assertIn('MINI', app.history_view.page.entries[0].native_label)
            await pilot.press('escape')
            app.action_sources()
            await self.until(pilot, lambda: app.sources_view is not None and app.sources_view.inspection is not None)
            self.assertEqual(app.sources_view.selection, target_key(job['target']))
            self.assertIn('/remote/MINI/', app.sources_view.inspection.context.sources[0].path)
            await pilot.press('escape')
            app.action_copy_snapshot()
            await self.until(pilot, lambda: isinstance(app.screen, NodeCopyPreview))
            data = app.screen.snapshot.to_dict()
            self.assertEqual(data['node_origin']['node_id'], job['target']['node_id'])
            self.assertEqual(data['node_origin']['attempt_id'], job['attempt_id'])
            self.assertIn('runtime-MINI', app.screen.text)
            await pilot.pause()
            original = app.screen.snapshot
            app.select(row_key(backends['MBP'].jobs[0]['target']))
            await pilot.click('#copy-refresh')
            self.assertEqual(app.screen.snapshot, original)
            remote_operations = [call[0] for call in backends['MINI'].calls]
            self.assertFalse(any('copy' in item or 'open' in item for item in remote_operations))
            for operation, _, target in backends['MINI'].calls:
                if operation in {'locations', 'history', 'history.release', 'sources.inspect'}:
                    self.assertEqual(target, job['target'])

    async def test_reconnection_invalidates_sources_and_history_binding(self):
        backends = self.backends()
        app = NodesApp(backends)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.until(pilot, lambda: len(app.rows) == 10)
            target = backends['MINI'].jobs[0]['target']
            app.select(row_key(target))
            app.action_sources()
            await self.until(pilot, lambda: app.sources_view is not None and app.sources_view.inspection is not None)
            app.session_sources[target_key(target)] = 'old-source-selection'
            backends['MINI'].session = uuid4().hex
            app.lanes['MINI'].next_poll = 0
            await self.until(pilot, lambda: app.sources_view.expired)
            self.assertNotIn(target_key(target), app.session_sources)

    async def test_control_uses_original_preview_and_separate_connection(self):
        backends = self.backends()
        fresh = FakeBackend('operation-connection')
        observed = []
        def apply(backend, preview, **kwargs):
            observed.append((backend, deepcopy(preview), kwargs))
            return {'status': 'ACCEPTED', 'request_id': 'stable-client-request'}
        app = NodesApp(backends, backend_factory=lambda owner: fresh)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.until(pilot, lambda: len(app.rows) == 10)
            job = backends['MINI'].jobs[0]
            app.select(row_key(job['target']))
            app.action_cancel()
            await self.until(pilot, lambda: isinstance(app.screen, NodeControlConfirm))
            preview = deepcopy(app.screen.preview)
            self.assertEqual(preview['target'], job['target'])
            with patch('cmw.nodes.client.apply_control', side_effect=apply, create=True):
                await pilot.click('#node-control-confirm')
                await self.until(pilot, lambda: bool(observed))
            self.assertIs(observed[0][0], fresh)
            self.assertEqual(observed[0][1], preview)
            self.assertTrue(observed[0][2]['confirm'])
            self.assertTrue(fresh.closed)
            self.assertFalse(any(call[0] == 'control' for call in backends['MINI'].calls))

    async def test_fetch_modal_uses_explicit_spec_destination_and_original_target(self):
        backends = self.backends()
        fresh = FakeBackend('fetch-connection')
        seen = []
        def fetch(backend, target, spec, destination, **kwargs):
            seen.append((backend, target, spec, destination))
            return {'status': 'REPLICA', 'destination': str(destination)}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'fetch.json'
            path.write_text(json.dumps({'schema': 1, 'files': [{'location_id': 'output', 'relative': 'summary.txt', 'path': 'summary.txt'}]}))
            app = NodesApp(backends, backend_factory=lambda owner: fresh, request_root=root / 'client')
            async with app.run_test(size=(100, 30)) as pilot:
                await self.until(pilot, lambda: len(app.rows) == 10)
                target = backends['MINI'].jobs[0]['target']
                app.select(row_key(target))
                app.action_files()
                await self.until(pilot, lambda: isinstance(app.screen, NodeFiles))
                await pilot.pause()
                fetch_button = app.screen.query_one('#node-files-fetch')
                self.assertTrue(fetch_button.region)
                self.assertTrue(app.screen.region.contains_region(fetch_button.region))
                await pilot.click('#node-files-fetch')
                self.assertIsInstance(app.screen, FetchDialog)
                app.screen.query_one('#fetch-spec', Input).value = str(path)
                app.screen.query_one('#fetch-destination', Input).value = str(root / 'new replica')
                with patch('cmw.nodes.client.fetch', side_effect=fetch, create=True):
                    await pilot.click('#fetch-confirm')
                    await self.until(pilot, lambda: bool(seen))
                self.assertIs(seen[0][0], fresh)
                self.assertEqual(seen[0][1], target)
                self.assertEqual(seen[0][2]['files'][0]['relative'], 'summary.txt')
                self.assertEqual(seen[0][3], root / 'new replica')
                self.assertTrue(fresh.closed)

    async def test_uncertain_control_queries_original_request_without_replay(self):
        backends = self.backends()
        applications, lookups = [], []
        class OperationBackend:
            def call(self, operation, params, **kwargs):
                lookups.append((operation, params))
                return {'result': {'request': {'result': {'status': 'ACCEPTED'}}}}
            def close(self):
                pass
        def apply(backend, preview, **kwargs):
            applications.append(kwargs['request_id'])
            raise NodeError('OUTCOME_UNKNOWN', 'reply lost after accepted mutation')
        app = NodesApp(backends, backend_factory=lambda owner: OperationBackend())
        async with app.run_test(size=(100, 30)) as pilot:
            await self.until(pilot, lambda: len(app.rows) == 10)
            app.select(row_key(backends['MINI'].jobs[0]['target']))
            app.action_cancel()
            await self.until(pilot, lambda: isinstance(app.screen, NodeControlConfirm))
            await pilot.pause()
            with patch('cmw.nodes.client.apply_control', side_effect=apply):
                await pilot.click('#node-control-confirm')
                await self.until(pilot, lambda: bool(lookups))
            self.assertEqual(len(applications), 1)
            self.assertEqual(lookups, [('request', {'request_id': applications[0]})])
            self.assertTrue(app.screen.query_one('#node-control-confirm').disabled)
            self.assertIn(applications[0], str(app.query_one('#message', Static).render()))

    async def test_source_activation_token_stays_in_own_observer_session(self):
        backends = self.backends()
        app = NodesApp(backends)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.until(pilot, lambda: len(app.rows) == 10)
            target = backends['MINI'].jobs[0]['target']
            app.select(row_key(target))
            app.action_sources()
            await self.until(pilot, lambda: app.sources_view is not None and app.sources_view.inspection is not None)
            view = app.sources_view
            view.request('validate', (SourceDescriptor('OUTCAR', '/remote/MINI/OUTCAR'),))
            await self.until(pilot, lambda: view.validation is not None)
            view.request('activate', (view.validation, True))
            await self.until(pilot, lambda: target_key(target) in app.session_sources)
            activations = [call for call in backends['MINI'].calls if call[0] == 'sources.activate']
            self.assertEqual(activations[0][1], {'validation_id': 'validate-MINI', 'confirmed': True})
            self.assertEqual(activations[0][2], target)
            self.assertFalse(any(call[0].startswith('sources.') for call in backends['MBP'].calls))

    async def test_read_only_controls_and_native_scientific_copy(self):
        backends = self.backends()
        backends['MINI'].connection['read_only'] = True
        copied = []
        app = NodesApp(backends, clipboard_backend=lambda text: (copied.append(text) or CopyResult('LOCAL_ACCEPTED', 'Copied locally')))
        async with app.run_test(size=(80, 24)) as pilot:
            await self.until(pilot, lambda: len(app.rows) == 10)
            app.select(row_key(backends['MINI'].jobs[0]['target']))
            app.collect_progress()
            await self.until(pilot, lambda: app.selected_snapshot('MINI', backends['MINI'].jobs[0]) is not None)
            await pilot.pause()
            app.action_hold()
            self.assertIn('read only', str(app.query_one('#message', Static).render()))
            self.assertFalse(any(call[0] == 'control.preview' for call in backends['MINI'].calls))
            await pilot.mouse_down('#progress', offset=(2, 1))
            await pilot.hover('#progress', offset=(5, 1))
            await pilot.mouse_up('#progress', offset=(5, 1))
            selection = app.screen.get_selected_text()
            self.assertTrue(selection)
            await pilot.press('ctrl+c')
            await self.until(pilot, lambda: bool(copied))
            self.assertEqual(copied, [selection])
            self.assertTrue(app.is_running)
