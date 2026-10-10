"""Normal registration accepts distinct semantics without generic engine branches."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.cli import main
from cmw.jobs.progress import resolve_context
from cmw.jobs.store import Store
from cmw.progress.formatting import progress_text
from cmw.progress.service import ObservationService
from tests.jobs.progress_extension_fixture import ENGINE, TEXT, register


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cmw-extension-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = Store(self.root/'state')
        self.source = self.root/'progress.synthetic'
        self.source.write_text(TEXT)
        self.job = self.store.add(argv=['/unexecuted/run-vasp.sh', '--input', str(self.root),
            '--output', str(self.root), '--binary', 'std', '--ranks', '1', '--ncore', '1', '--kpar', '1',
            '--mpi-mode', 'synthetic', '--restart', 'none', '--timeout', '10', '--stop-before', '0',
            '--managed-foreground'], cwd=self.root, name='Extension fixture', engine=ENGINE, hold=True)
        from cmw.progress.registry import _REGISTRY
        registry = patch.dict(_REGISTRY)
        registry.start()
        self.addCleanup(registry.stop)
        register()

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['jobs', '--state', str(self.store.root), 'progress', self.job['display_id'], *args])
        self.assertEqual((code, err.getvalue()), (0, ''))
        return out.getvalue()

    def test_registered_default_sources_three_levels_semantic_identity_and_timing(self):
        before = self.store.path.read_bytes()
        snapshot = json.loads(self.invoke('--json'))
        self.assertEqual(snapshot['context']['sources'][0]['role'], 'synthetic')
        self.assertEqual(len(snapshot['iteration']), 3)
        metrics = [item['latest'] for item in snapshot['metrics']]
        self.assertNotEqual(metrics[0]['definition'], metrics[1]['definition'])
        self.assertEqual([item['value'] for item in metrics], [.002, .003, .2])
        self.assertEqual(metrics[2]['unit'], 'C/s')
        self.assertIsNone(metrics[2]['criterion'])
        self.assertTrue(all(item['previous'] is None for item in snapshot['metrics']))
        roots = json.loads(self.invoke('--history', '--json'))
        self.assertEqual([(row['stage'], row['segment']) for row in roots['entries']], [('OPT', 1), ('SP', 2)])
        self.assertEqual(roots['entries'][0]['timings'][0]['value'], 3.5)
        self.assertIsNone(roots['entries'][1]['timings'][0]['value'])
        outer = json.loads(self.invoke('--history', '--parent', roots['entries'][0]['entry_id'], '--json'))['entries'][0]
        self.assertEqual(outer['timings'], [])
        inner = json.loads(self.invoke('--history', '--parent', outer['entry_id'], '--json'))['entries']
        self.assertEqual([len(row['context']) for row in inner], [3, 3])
        self.assertEqual([row['metrics'][2]['value'] for row in inner], [.4, .3])
        text = self.invoke()
        for label in ('Maximum component', 'Atomic vector norm', 'Charge flux', 'C/s', 'SP / inner solver'):
            self.assertIn(label, text)
        self.assertNotIn('Fmax', text)
        self.assertEqual(before, self.store.path.read_bytes())

    def test_stage_segment_and_source_resets_keep_history_but_never_compare_unlike_context(self):
        self.source.write_text('\n'.join(TEXT.splitlines()[:2])+'\n')
        context = resolve_context(self.store, self.job['display_id'])
        service = ObservationService()
        self.addCleanup(service.close)
        first = service.poll(context)
        self.assertEqual(first.metrics[0].previous.value, .004)
        with self.source.open('a') as output:
            output.write(TEXT.splitlines()[2]+'\n')
        second = service.poll(context)
        self.assertTrue(all(series.previous is None for series in second.metrics))
        self.assertIn('SP / inner solver', progress_text(second, compact=True, width=140))
        self.assertEqual(len(service.history_page(context).entries), 2)
        replacement = self.root/'replacement'
        replacement.write_text('OPT,3,1,1,1,0.007,?\n')
        replacement.replace(self.source)
        third = service.poll(context)
        self.assertTrue(all(series.previous is None for series in third.metrics))
        history = service.history_page(context)
        self.assertEqual([(entry.stage, entry.segment) for entry in history.entries], [('OPT', 3)])
        self.assertIsNone(history.entries[0].timings[0].value)
        service.close()
        self.assertFalse(service._contexts)
        self.assertEqual(service._inflight, 0)


if __name__ == '__main__':
    unittest.main()
