"""Read-only Jobs progress and a deliberately non-VASP extension dialect."""
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cmw.cli import main
from cmw.jobs.store import Store
from cmw.progress.models import Criterion, IterationLevel, Metric, MetricSeries, ProgressSnapshot, RunContext
from cmw.progress.registry import register_adapter


class FictionalAdapter:
    """A fixture with arbitrary nesting and a metric absent from VASP."""
    def __init__(self, context):
        self.context = context
        self.latest = self.previous = None
        self.changed_at = None

    def update(self, updates, now):
        for update in updates:
            for line in update.lines:
                value = float(line.text)
                level = 1 if self.latest is None else self.latest.context[-1].index + 1
                levels = (IterationLevel('geometry', 4, 'geometry evaluation'),
                          IterationLevel('outer', 2, 'outer iteration'),
                          IterationLevel('inner', level, 'inner iteration', 50))
                self.previous, self.latest = self.latest, Metric('fictional.charge_flux', 'Charge flux',
                    'Charge transferred per unit simulated time', 'C/s', value, levels,
                    evidence=(line.evidence,), observed_at=now, priority=1, comparison_context=levels[:-1])
                self.changed_at = now
        return ProgressSnapshot(self.context, 'fictional-fixture/v1', status='OBSERVING', phase='Inner iteration',
            iteration=self.latest.context if self.latest else (),
            metrics=(MetricSeries(self.latest, self.previous),) if self.latest else (),
            completeness='COMPLETE' if self.latest else 'PENDING', read_health='OK',
            last_new_evidence_at=self.changed_at)


class ProgressCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-progress-cli-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root / 'state')
        self.job = self.store.add(argv=['/unexecuted/opaque-wrapper'], cwd=self.root,
                                  name='Synthetic progress', engine='FICTIONAL', hold=True)
        self.source = self.root / 'charge-flux.txt'
        self.source.write_text('3.75\n3.25\n')
        from cmw.progress.registry import _REGISTRY
        registration = patch.dict(_REGISTRY)
        registration.start()
        self.addCleanup(registration.stop)
        register_adapter('FICTIONAL', FictionalAdapter)

    def invoke(self, *args, state=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['jobs', '--state', str(state or self.store.root), 'progress', *args])
        return code, out.getvalue(), err.getvalue()

    def test_fictional_three_level_extension_uses_normal_cli_and_json_read_only(self):
        before = self.store.path.read_bytes()
        args = (self.job['display_id'], '--source', f'flux={self.source}')
        code, text, err = self.invoke(*args, '--json')
        self.assertEqual((code, err), (0, ''))
        payload = json.loads(text)
        self.assertEqual(payload['context']['attempt_id'], self.job['attempt_id'])
        self.assertEqual(payload['context']['association'], 'explicit-caller-declaration')
        self.assertEqual(len(payload['iteration']), 3)
        self.assertEqual(payload['metrics'][0]['latest']['unit'], 'C/s')
        self.assertEqual(payload['metrics'][0]['previous']['value'], 3.75)
        self.assertFalse(payload['context']['process_ownership'])
        code, text, _ = self.invoke(*args)
        self.assertEqual(code, 0)
        for part in ('Charge flux [C/s]', '3.25', '3.75', 'geometry evaluation 4', 'outer iteration 2', 'inner iteration 2', 'target', str(self.source)):
            self.assertIn(part.lower(), text.lower())
        self.assertEqual(before, self.store.path.read_bytes())
        state = self.store.snapshot()
        self.assertFalse(state['controller']['online'])
        self.assertEqual(state['jobs'][0]['status'], 'Hold')

    def test_unknown_engine_and_opaque_source_binding_are_explicit(self):
        code, text, _ = self.invoke(self.job['display_id'], '--engine', 'NO_SUCH_ADAPTER', '--json')
        self.assertEqual((code, json.loads(text)['status']), (0, 'ADAPTER_UNAVAILABLE'))
        code, text, _ = self.invoke(self.job['display_id'], '--json')
        self.assertEqual((code, json.loads(text)['status']), (0, 'SOURCE_UNRESOLVED'))
        for source in ('bad', 'flux=relative.log', '=/' ):
            code, text, _ = self.invoke(self.job['display_id'], '--source', source, '--json')
            self.assertEqual((code, json.loads(text)['code']), (2, 'JOBS_ERROR'))
        missing = self.root / 'missing-state'
        code, _, _ = self.invoke('J1.1', state=missing)
        self.assertEqual(code, 2)
        self.assertFalse(missing.exists())

    def test_core_progress_command_does_not_import_optional_ui(self):
        script = '''import importlib.abc, sys
class BlockUI(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'textual', 'rich'}:
            raise AssertionError('Optional console import: ' + fullname)
sys.meta_path.insert(0, BlockUI())
from cmw.cli import main
raise SystemExit(main(sys.argv[1:]))
'''
        completed = subprocess.run([sys.executable, '-c', script, 'jobs', '--state', str(self.store.root),
                                    'progress', self.job['display_id'], '--json'],
                                   capture_output=True, text=True, timeout=15,
                                   env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)['status'], 'ADAPTER_UNAVAILABLE')


class ProgressFormattingTests(unittest.TestCase):
    def test_native_and_diagnostic_targets_uncertainty_and_signed_evidence_stay_separate(self):
        from cmw.progress.formatting import progress_text
        metric = Metric('fictional.residual', '|Residual|', 'Synthetic residual magnitude', 'C/s', .01,
            signed_value=-.01, printed='-.010000', precision=.0000005, qualification='provisional',
            criterion=Criterion(.01, 'C/s', '<', 'effective engine setting'), relation='PROVISIONAL_UNCERTAIN',
            diagnostic_targets=(Criterion(.005, 'C/s', '<', 'caller policy', kind='diagnostic'),))
        snapshot = ProgressSnapshot(RunContext('fictional-format', 'FICTIONAL'), 'fixture', metrics=(MetricSeries(metric),))
        compact = progress_text(snapshot, compact=True, width=120, now=1)
        self.assertIn('uncertain', compact)
        self.assertIn('target provisional uncertain <', compact)
        full = progress_text(snapshot, detailed=True, now=1)
        self.assertIn('signed value -0.01', full)
        self.assertIn('Caller diagnostic target: < 0.005 C/s; caller policy', full)
        self.assertIn('Engine criterion:', full)
        self.assertNotIn('PASS', full)

    def test_unavailable_values_and_three_level_contexts_are_not_zero_or_fixed_depth(self):
        from cmw.progress.formatting import progress_text
        levels = tuple(IterationLevel(kind, index, kind) for kind, index in (('geometry', 4), ('outer', 2), ('inner', 9)))
        metric = Metric('fictional.flux', 'Flux', 'Missing native observation', 'C/s', None, levels)
        snapshot = ProgressSnapshot(RunContext('fictional-format', 'FICTIONAL'), 'fixture', iteration=levels,
                                    metrics=(MetricSeries(metric),))
        text = progress_text(snapshot, compact=True, width=80, now=1)
        self.assertIn('— @4/2/9', text)
        self.assertIn('target —', text)
        self.assertTrue(all(len(line) <= 80 for line in text.splitlines()))


if __name__ == '__main__':
    unittest.main()
