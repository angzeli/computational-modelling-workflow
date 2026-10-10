"""Explicit stage sources are a generic, read-only CLI declaration."""
import json
from cmw.progress.models import ProgressSnapshot
import unittest
from tests.jobs import test_progress_cli as base
from unittest.mock import patch


class StageSourceCliTests(unittest.TestCase):
    setUp = base.ProgressCliTests.setUp
    invoke = base.ProgressCliTests.invoke

    def test_stage_sources_preserve_order_and_do_not_change_jobs(self):
        from cmw.progress.service import ObservationService
        before = self.store.path.read_bytes()
        second = self.root / 'second.txt'
        second.write_text('8.5\n')
        seen = []
        def observe(service, context):
            seen.append(context)
            return ProgressSnapshot(context, 'stage-declaration-test')
        with patch.object(ObservationService, 'poll', observe):
            code, out, err = self.invoke(self.job['display_id'], '--json',
                '--source', f'flux@prepare={self.source}',
                '--source', f'flux@solve={second}')
        self.assertEqual((code, err), (0, ''))
        self.assertEqual([(s.role, s.stage, s.path) for s in seen[0].sources], [
            ('flux:prepare', 'prepare', str(self.source)),
            ('flux:solve', 'solve', str(second))])
        self.assertEqual(json.loads(out)['context']['binding_status'], 'BOUND')
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_stage_declaration_rejects_empty_or_ambiguous_stage(self):
        for role in ('flux@', '@OPT', 'flux@OPT@SP'):
            with self.subTest(role=role):
                code, out, _ = self.invoke(self.job['display_id'], '--json',
                    '--source', f'{role}={self.source}')
                self.assertEqual((code, json.loads(out)['code']), (2, 'JOBS_ERROR'))
