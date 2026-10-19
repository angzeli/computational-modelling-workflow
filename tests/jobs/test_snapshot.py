"""Core snapshots use synthetic cached evidence and no clipboard or scientific I/O."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from cmw.jobs.clipboard import CopyResult, copy_text, verified_local_macos
from cmw.jobs.snapshot import capture_snapshot, render_snapshot
from cmw.jobs.store import Store
from cmw.progress.models import (Criterion, Evidence, IterationLevel, Metric, MetricSeries,
                                 ProgressSnapshot, RunContext, SettingObservation, SourceState, Verification)


def fixture(job):
    context = RunContext('synthetic-bound', 'FICTIONAL', attempt_id=job['attempt_id'], job_id=job['id'])
    row = (IterationLevel('outer', 1, 'outer'), IterationLevel('scf', 8, 'SCF'))
    metric = Metric('fixture.de', '|dE|', 'synthetic signed difference magnitude', 'eV', 3.1087,
                    row, printed='+3.10870000', signed_value=3.1087, qualification='source-local',
                    evidence=(Evidence('stdout', '/synthetic private/output.log', 1, 8, 8, 60, 80),),
                    observed_at=100, criterion=Criterion(1e-5, 'eV', '<', 'effective OUTCAR'))
    return ProgressSnapshot(context, 'fixture', iteration=(*row[:-1], replace(row[-1], index=9)),
        metrics=(MetricSeries(metric),), sources=(SourceState('stdout', '/synthetic private/output.log', 1, offset=80),),
        last_new_evidence_at=100, last_success_at=111,
        verification=(Verification('electronic', 'PARTIAL', 'stdout', 'outcar', 8, 6, 9,
                                    fields=('energy',), reason='Rows 7–8 source-only'),),
        settings=(SettingObservation('EDIFF', 1e-5, 'effective OUTCAR'),),
        messages=('Synthetic warning; historical provenance unverified.',))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'state')
        self.job = self.store.add(argv=['/unexecuted/wrapper', 'private argument'], env={'SECRET': 'excluded'},
                                  cwd=temporary.name, name='Long name ' * 40 + ' 繁體 [bold]\x1b[31m', hold=True)

    def test_capture_is_frozen_complete_safe_and_qualified(self):
        snapshot = capture_snapshot(self.job, progress=fixture(self.job), captured_at=112)
        self.job['name'] = 'changed later'
        data = snapshot.to_dict()
        data['job']['name'] = 'cannot mutate capture'
        text = render_snapshot(snapshot)
        self.assertIn('Long name ' * 40, text)
        self.assertIn('SCF 9', text)
        self.assertIn('SCF 8', text)
        self.assertIn('3.10870000', text)
        self.assertIn('checked through 6', text)
        self.assertIn('Rows 7–8 source-only', text)
        self.assertIn('EDIFF: 1e-05', text)
        self.assertIn('last_new_evidence_at: 100 (12s ago)', text)
        self.assertIn('last_success_at: 111 (1s ago)', text)
        self.assertIn('Scientific acceptance: not assessed', text)
        for mode in ('summary', 'diagnostics', 'json'):
            rendered = render_snapshot(snapshot, mode=mode)
            self.assertNotIn('private argument', rendered)
            self.assertNotIn('SECRET', rendered)
            self.assertNotIn('\x1b', rendered)
            self.assertIn('historical provenance unverified', rendered)
        self.assertTrue(snapshot.to_dict()['runtime']['python'])
        self.assertNotIn('changed later', text)

    def test_nested_execution_diagnostics_do_not_export_environment_or_commands(self):
        execution = {'attempt_id': self.job['attempt_id'], 'summary': 'Synthetic scoped worker',
                     'argv': ['private argument'], 'evidence': {'worker': {'env': {'KEY': 'secret value'},
                                  'cmdline': ['private child argument'], 'pid': 12}}}
        capture = capture_snapshot(self.job, execution=execution)
        text = render_snapshot(capture, mode='diagnostics')
        self.assertNotIn('private argument', text)
        self.assertNotIn('private child argument', text)
        self.assertNotIn('secret value', text)
        self.assertIn('"pid": 12', text)
        with self.assertRaisesRegex(ValueError, 'different attempt'):
            capture_snapshot(self.job, execution={**execution, 'attempt_id': 'other'})

    def test_redaction_retains_identity_and_no_cross_attempt_capture(self):
        snapshot = capture_snapshot(self.job, progress=fixture(self.job), captured_at=112)
        redacted = json.loads(render_snapshot(snapshot, mode='json', redact_paths=True))
        self.assertEqual(redacted['job']['attempt_id'], self.job['attempt_id'])
        self.assertNotIn('/synthetic private/output.log', json.dumps(redacted))
        self.assertTrue(redacted['progress']['sources'][0]['path'].startswith('<path'))
        bad = replace(fixture(self.job), context=replace(fixture(self.job).context, attempt_id='other'))
        with self.assertRaisesRegex(ValueError, 'different attempt'):
            capture_snapshot(self.job, progress=bad)


class ClipboardTests(unittest.TestCase):
    def test_local_verification_excludes_remote_and_unknown_paths(self):
        options = dict(platform='darwin', environ={'TERM_PROGRAM': 'Apple_Terminal'}, console_uid=501, uid=501, tty=True)
        self.assertTrue(verified_local_macos(**options))
        for extra in ({'SSH_CONNECTION': 'remote'}, {'TMUX': 'session'}):
            self.assertFalse(verified_local_macos(**{**options, 'environ': {**options['environ'], **extra}}))
        self.assertFalse(verified_local_macos(**{**options, 'console_uid': 502}))
        self.assertFalse(verified_local_macos(**{**options, 'tty': False}))

    def test_only_stubbed_local_stdin_and_distinct_osc52_results(self):
        runner = Mock(return_value=Mock(returncode=0))
        payload = 'synthetic $(do not execute) 繁體\nsecond'
        self.assertEqual(copy_text(payload, local_verified=True, runner=runner).status, 'LOCAL_ACCEPTED')
        self.assertEqual(runner.call_args.args, (['/usr/bin/pbcopy'],))
        self.assertEqual(runner.call_args.kwargs['input'], payload)
        self.assertNotIn('shell', runner.call_args.kwargs)
        osc = Mock()
        self.assertEqual(copy_text(payload, local_verified=False, osc52=osc).status, 'OSC52_REQUESTED')
        osc.assert_called_once_with(payload)
        self.assertEqual(copy_text(payload, local_verified=False).status, 'UNAVAILABLE')
        runner.side_effect = OSError('synthetic')
        self.assertEqual(copy_text(payload, local_verified=True, runner=runner).status, 'FAILED')
        self.assertEqual(copy_text('x' * (1024 * 1024 + 1), local_verified=True, runner=runner).status, 'UNAVAILABLE')
