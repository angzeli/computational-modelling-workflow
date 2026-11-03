"""Invented managed VASP streams through a persistent stdio endpoint and portal."""
from pathlib import Path
import unittest

from cmw.jobs.store import Store
from cmw.nodes.codec import decode_typed
from cmw.progress.formatting import progress_text
from cmw.progress.models import ProgressSnapshot
from tests.nodes import test_endpoint
from tests.nodes.test_portal import NodesApp
from tests.periodic.vasp.result_fixtures import native_texts

if NodesApp is not None:
    from cmw.nodes.portal import row_key, target_key
    from textual.widgets import Static


def energy(index):
    if index <= 34:
        return -1264.75746284 + .001 * (34 - index)
    return -1264.75780537 - .001 * (index - 35)


def dav(index):
    change = -.00034253 if index == 35 else -.001
    return f'DAV: {index} {energy(index):.8f} {change:.8f} -.000014342 12 .00166 .0116\n'


def managed_fixture(store, root):
    inputs, output = root / 'inputs', root / 'output'
    inputs.mkdir(); output.mkdir()
    texts = native_texts(nelm=300)
    (inputs / 'INCAR').write_text(texts['INCAR'])
    prefix = texts['OUTCAR'].split('--------------------------------------- Iteration')[0]
    rows = ''.join(f'--------------------------------------- Iteration 1({i:4d}) ---------------------------------------\n'
                   ' EDDAV: cpu time 0.0010: real time 0.0020\n'
                   f' free energy TOTEN = {energy(i):.8f} eV\n' for i in range(1, 35))
    (output / 'OUTCAR').write_text(prefix + rows)
    (output / 'OSZICAR').write_text('')
    (output / 'stdout.log').write_text(''.join(dav(i) for i in range(1, 36)))
    job = store.add(argv=['/not-executed/run-vasp.sh', '--input', str(inputs), '--output', str(output),
                          '--managed-foreground'], cwd=inputs, engine='VASP', name='invented DAV stream', hold=True)
    outer = Path(job['logs']['stdout'])
    outer.parent.mkdir(parents=True, exist_ok=True)
    outer.write_text(dav(999))
    return job, output


@unittest.skipIf(NodesApp is None, 'Jobs UI extra unavailable')
class ManagedStdoutEndpointTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_endpoint.EndpointTests.setUp
    node = test_endpoint.EndpointTests.node

    async def until(self, pilot, predicate):
        for _ in range(240):
            if predicate():
                return
            await pilot.pause(.05)
        self.fail('Persistent portal observation did not reach the expected frontier')

    async def test_automatic_stdout_same_selected_attempt_partial_then_complete(self):
        backend, config, _, _ = self.node('mini')
        store = Store(config['queues']['research']['state'])
        job, output = managed_fixture(store, self.root / 'mini' / 'calculation')
        status = backend.call('status')
        target = next(item['target'] for item in status['result']['state']['jobs'] if item['id'] == job['id'])
        first = backend.call('progress', target=target)
        snapshot = decode_typed(first['result']['snapshot'], ProgressSnapshot)
        sources = {source.role: source for source in snapshot.context.sources}
        self.assertIn('stdout', sources)
        self.assertEqual(sources['stdout'].path, str(output / 'stdout.log'))
        self.assertNotEqual(sources['stdout'].path, job['logs']['stdout'])
        self.assertEqual(first['session_id'], status['session_id'])

        app = NodesApp({'mini': backend})
        async with app.run_test(size=(180, 50)) as pilot:
            await self.until(pilot, lambda: row_key(target) in app.rows and not app.navigation_pending())
            app.select(row_key(target))
            app.render_state()
            selected_job = app.rows[row_key(target)][1]
            def current():
                return app.selected_snapshot('mini', selected_job)
            def frontier():
                snap = current()
                return snap.verification[0].observed_frontier if snap and snap.verification else None
            await self.until(pilot, lambda: frontier() == 35)
            snap = current()
            verification = snap.verification[0]
            self.assertEqual((verification.source_role, verification.status, verification.checked_frontier),
                             ('stdout', 'PARTIAL', 34))
            metrics = {series.latest.semantic_id: series.latest for series in snap.metrics}
            for suffix, value in (('free_energy', -1264.75780537), ('dE', -.00034253)):
                metric = metrics['vasp.electronic.' + suffix]
                self.assertAlmostEqual(metric.signed_value if suffix == 'dE' else metric.value, value, places=10)
                self.assertEqual(metric.context[-1].index, 35)
                self.assertEqual(metric.qualification, 'source-local')
            self.assertEqual(snap.scientific_validation, 'not-assessed')
            text = progress_text(snap, framed=True, width=180, max_lines=14)
            self.assertIn('OUTCAR SCF 34', text)
            self.assertIn('35', text)
            self.assertIn('3.4253', text)
            self.assertIn('-1264.75780537 eV @SCF35', text)
            self.assertIn('stdout SCF 35', text)
            self.assertIn('outcar checked 34', text)
            self.assertIn('signed value -0.00034253', progress_text(snap, detailed=True))
            for width in (68, 95, 135):
                compact = progress_text(snap, framed=True, width=width, max_lines=6)
                self.assertLessEqual(len(compact.splitlines()), 6)
                self.assertTrue(any('E (SCF)' in line and '@SCF35' in line for line in compact.splitlines()))
            rendered = str(app.query_one('#progress', Static).render())
            self.assertIn('OUTCAR SCF 34', rendered)
            self.assertIn('-1264.75780537 eV @SCF35', rendered)
            selection, generation = app.selected_key, app.selection_generation
            session = app.lanes['mini'].session
            cache_key = target_key(target)
            sequence = app.lanes['mini'].cache[cache_key]['progress'][0]['sequence']
            with (output / 'stdout.log').open('a') as handle:
                handle.write(dav(36).rstrip('\n'))
            # Wait for a real scheduled progress response, not a status receipt.
            await self.until(pilot, lambda: app.lanes['mini'].cache[cache_key]['progress'][0]['sequence'] > sequence
                and any(source.role == 'stdout' and source.pending_bytes > 0 for source in current().sources))
            self.assertEqual(frontier(), 35)
            with (output / 'stdout.log').open('a') as handle:
                handle.write('\n')
            await self.until(pilot, lambda: frontier() == 36)
            self.assertIn('-1264.75880537 eV @SCF36', str(app.query_one('#progress', Static).render()))
            self.assertEqual(current().verification[0].checked_frontier, 34)
            self.assertEqual((app.selected_key, app.selection_generation), (selection, generation))
            self.assertEqual(app.lanes['mini'].session, session)
            self.assertEqual(session, first['session_id'])
