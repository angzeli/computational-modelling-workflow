"""Synthetic historical DAV excerpt through Jobs, real readers and presentation.

These numbers reproduce the user's historical sample in an invented run; they
are never treated as live J17 evidence or execution authorization.
"""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.cli import main
from cmw.jobs.progress import resolve_context
from cmw.jobs.snapshot import capture_snapshot, render_snapshot
from cmw.jobs.store import Store
from cmw.progress.formatting import history_entry_text, progress_text
from cmw.progress.models import SourceDescriptor
from cmw.progress.service import ObservationService
from tests.periodic.vasp.result_fixtures import native_texts


HEADER = ' N       E                     dE             d eps       ncg     rms          rms(c)\n'
DAV_ROWS = (
    'DAV:   1     0.141625777076E+05    0.14163E+05   -0.58996E+05  4008   0.154E+03\n',
    'DAV:   2     0.158364101942E+04   -0.12579E+05   -0.12303E+05  4176   0.350E+02\n',
    'DAV:   3    -0.565682843405E+03   -0.21493E+04   -0.21257E+04  4464   0.185E+02\n',
    'DAV:   4    -0.663542211760E+03   -0.97859E+02   -0.97060E+02  4048   0.415E+01\n',
    'DAV:   5    -0.667705081337E+03   -0.41629E+01   -0.41595E+01  4848   0.875E+00    0.580E+01\n',
    'DAV:   6    -0.619608778533E+03    0.48096E+02   -0.83007E+01  4560   0.363E+01    0.416E+01\n',
    'DAV:   7    -0.627287617078E+03   -0.76788E+01   -0.61033E+01  5184   0.162E+01    0.229E+01\n',
    'DAV:   8    -0.624178913665E+03    0.31087E+01   -0.29332E+01  5112   0.189E+01    0.141E+01\n',
)
SIZES = ((77, 24), (80, 24), (100, 30), (140, 40))


def outcar_row(index):
    energy = float(DAV_ROWS[index - 1].split()[2])
    return (f'--------------------------------------- Iteration      1({index:4d})  ---------------------------------------\n'
            ' EDDAV: cpu time 0.0010: real time 0.0020\n'
            f' free energy    TOTEN = {energy:.12f} eV\n')


def setup_case(test):
    temporary = tempfile.TemporaryDirectory(prefix='cmw-j17-path-')
    test.addCleanup(temporary.cleanup)
    test.root = Path(temporary.name).resolve()
    text = native_texts(nelm=120)
    prefix = text['OUTCAR'].split('--------------------------------------- Iteration')[0]
    (test.root / 'OUTCAR').write_text(prefix + ''.join(outcar_row(i) for i in range(1, 7)))
    (test.root / 'OSZICAR').write_text('')
    (test.root / 'stdout.log').write_text('')
    test.sources = tuple(SourceDescriptor(role, str(test.root / filename))
                         for role, filename in (('outcar', 'OUTCAR'), ('oszicar', 'OSZICAR'), ('stdout', 'stdout.log')))
    test.store = Store(test.root / 'state')
    test.job = test.store.add(argv=['/synthetic/never-executed'], cwd=test.root,
        engine='VASP', name='Invented historical DAV observation', hold=True, progress_sources=test.sources)
    test.context = resolve_context(test.store, test.job['id'], expected_attempt_id=test.job['attempt_id'])
    test.now = 1000.
    test.service = ObservationService(clock=lambda: test.now, monotonic_clock=lambda: test.now)
    test.addCleanup(test.service.close)


def metric(snapshot, suffix):
    return next(item.latest for item in snapshot.metrics if item.latest.semantic_id == 'vasp.electronic.' + suffix)


def metric_facts(payload):
    return {item['latest']['semantic_id']: (item['latest']['value'], item['latest']['signed_value'],
             item['latest']['qualification'], tuple(level['index'] for level in item['latest']['context']))
            for item in payload['metrics']}


class J17DataPathTests(unittest.TestCase):
    setUp = setup_case

    def append(self, name, text):
        with (self.root / name).open('a') as handle:
            handle.write(text)
        self.now += 1

    def test_growing_native_rows_reach_card_details_history_copy_and_cli(self):
        before = self.store.path.read_bytes()
        self.assertEqual(self.context.attempt_id, self.job['attempt_id'])
        self.assertEqual({item.role for item in self.context.sources}, {'outcar', 'oszicar', 'stdout'})
        target_only = self.service.poll(self.context)
        self.assertFalse(target_only.metrics)
        self.assertIn('EDIFF (both) <1e-06 eV', progress_text(target_only, width=80, now=self.now))

        split = len(DAV_ROWS[7]) // 2
        self.append('stdout.log', HEADER + ''.join(DAV_ROWS[:7]) + DAV_ROWS[7][:split])
        partial_line = self.service.poll(self.context)
        self.assertEqual(metric(partial_line, 'dE').context[-1].index, 7)
        self.assertGreater(next(item.pending_bytes for item in partial_line.sources if item.role == 'stdout'), 0)
        self.append('stdout.log', DAV_ROWS[7][split:])
        local = self.service.poll(self.context)
        self.assertEqual((local.verification[0].source_role, local.verification[0].status,
                          local.verification[0].observed_frontier, local.verification[0].checked_frontier),
                         ('stdout', 'PARTIAL', 8, 6))
        for suffix, absolute, signed in (('free_energy', -624.178913665, None),
                                        ('dE', 3.1087, 3.1087), ('d_eps', 2.9332, -2.9332),
                                        ('rms', 1.89, None), ('rms_c', 1.41, None)):
            value = metric(local, suffix)
            self.assertEqual((value.value, value.signed_value), (absolute, signed))
            self.assertEqual(value.context[-1].index, 8)
            self.assertEqual(value.qualification, 'source-local')
            evidence = value.evidence[0]
            self.assertEqual((evidence.source_role, evidence.path, evidence.line_start),
                             ('stdout', str(self.root / 'stdout.log'), 9))
            raw = (self.root / 'stdout.log').read_bytes()[evidence.byte_start:evidence.byte_end].decode()
            self.assertEqual(raw, DAV_ROWS[7])
        self.assertGreater(next(item.bytes_read for item in local.sources if item.role == 'stdout'), 0)
        for width, _height in SIZES:
            card = progress_text(local, width=width, now=self.now)
            for value in ('3.1087', '2.9332', '1.89', '@SCF8', 'EDIFF (both) <1e-06 eV'):
                self.assertIn(value, card)
        initial_rows = {entry.native_label: entry for entry in self.service.history(self.context, parent_id='*', limit=100).entries
                        if entry.kind == 'electronic'}
        self.assertEqual(len(initial_rows), 8)
        self.assertEqual(next(item for item in initial_rows['8'].metrics
                              if item.semantic_id == 'vasp.electronic.dE').value, 3.1087)
        self.assertIn('source-local', history_entry_text(initial_rows['8']))
        for text in (progress_text(local, detailed=True, now=self.now),
                     render_snapshot(capture_snapshot(self.job, progress=local, captured_at=self.now))):
            for value in ('0.31087E+01', '0.29332E+01', '0.189E+01', 'source-local', 'stdout'):
                self.assertIn(value, text)
        frozen = capture_snapshot(self.job, progress=local, captured_at=self.now)
        self.assertEqual(metric_facts(json.loads(render_snapshot(frozen, mode='json'))['progress']),
                         metric_facts(local.to_dict()))

        self.append('OUTCAR', outcar_row(7) + outcar_row(8) +
                    '--------------------------------------- Iteration      1(   9)  ---------------------------------------\n')
        matched = self.service.poll(self.context)
        self.assertEqual(matched.verification[0].checked_frontier, 8)
        self.assertEqual(matched.iteration[-1].index, 9)
        self.assertEqual(metric(matched, 'dE').context[-1].index, 8)
        self.assertEqual(metric(matched, 'dE').qualification, 'row-correspondence')
        self.assertEqual(metric(matched, 'dE').value, 3.1087)
        self.assertEqual(metric_facts(frozen.to_dict()['progress']), metric_facts(local.to_dict()))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['jobs', '--state', str(self.store.root), 'progress', self.job['display_id'], '--json'])
        self.assertEqual((code, err.getvalue()), (0, ''))
        self.assertEqual(metric_facts(json.loads(out.getvalue())), metric_facts(matched.to_dict()))
        self.append('OSZICAR', HEADER + ''.join(DAV_ROWS))
        duplicated = self.service.poll(self.context)
        self.assertEqual(duplicated.verification[1].checked_frontier, 8)
        rows = {entry.native_label: entry for entry in self.service.history(self.context, parent_id='*', limit=100).entries
                if entry.kind == 'electronic'}
        self.assertEqual(len(rows), 9)  # Native SCF9 is a header, with no invented electronic row.
        self.assertEqual(rows['8'].entry_id, initial_rows['8'].entry_id)
        self.assertEqual(metric(duplicated, 'dE').context[-1].index, 8)
        self.assertEqual(self.store.path.read_bytes(), before)
        state = self.store.snapshot()
        self.assertFalse(state['controller']['online'])
        self.assertFalse(state['controller']['dispatch'])
        self.assertEqual(state['jobs'][0]['status'], 'Hold')


try:
    from cmw.jobs.copy_ui import CopyPreview
    from cmw.jobs.tui import JobsApp, Inspect
    from textual.widgets import Select, Static, TextArea
except ImportError:
    JobsApp = None


@unittest.skipIf(JobsApp is None, 'Optional Jobs console not installed')
class J17DataPathUiTests(unittest.IsolatedAsyncioTestCase):
    setUp = setup_case

    async def test_real_native_observation_in_selected_card_details_and_copy(self):
        (self.root / 'stdout.log').write_text(HEADER + ''.join(DAV_ROWS))
        before = self.store.path.read_bytes()
        app = JobsApp(self.store, clock=lambda: self.now)
        with patch.object(JobsApp, 'collect_activity'), patch.object(JobsApp, 'collect_execution'), \
                patch('subprocess.Popen', side_effect=AssertionError('No calculation launch')):
            async with app.run_test(size=SIZES[0]) as pilot:
                for _ in range(300):
                    await pilot.pause(.01)
                    if not app.progress_busy and app.progress_snapshot is not None:
                        break
                self.assertIsNotNone(app.progress_snapshot)
                self.assertEqual(metric(app.progress_snapshot, 'dE').value, 3.1087)
                for size in SIZES:
                    await pilot.resize_terminal(*size)
                    await pilot.pause()
                    card = str(app.query_one('#progress', Static).render())
                    for value in ('3.1087', '2.9332', '1.89', '@SCF8'):
                        self.assertIn(value, card)
                    self.assertLessEqual(app.query_one('#progress').region.bottom, size[1])
                    self.assertLessEqual(app.query_one('#shortcuts').region.bottom, size[1])
                await pilot.press('enter')
                self.assertIsInstance(app.screen, Inspect)
                details = str(app.screen.query_one('#body', Static).render())
                for value in ('0.31087E+01', '0.29332E+01', 'source-local'):
                    self.assertIn(value, details)
                await pilot.press('escape', 'y')
                self.assertIsInstance(app.screen, CopyPreview)
                preview = app.screen
                for value in ('0.31087E+01', '0.29332E+01', 'source-local'):
                    self.assertIn(value, preview.query_one(TextArea).text)
                preview.query_one(Select).value = 'json'
                await pilot.pause()
                self.assertEqual(metric_facts(json.loads(preview.query_one(TextArea).text)['progress']),
                                 metric_facts(app.progress_snapshot.to_dict()))
                await pilot.press('escape', 'q')
        self.assertEqual(self.store.path.read_bytes(), before)
