"""Regression evidence for full IFCT fields versus compact stage ellipsis."""
from dataclasses import replace
import unittest

from cmw.progress.card import card_text
from cmw.progress.formatting import progress_text
from tests.campaign.analysis_oracle_v2 import analysis_evidence, analysis_painted, analysis_details


class AnalysisOracleTests(unittest.TestCase):
    def setUp(self):
        from tests.progress.test_multiwfn import MultiwfnProgressTests
        self.fixture = MultiwfnProgressTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        from pathlib import Path
        Path(self.fixture.sources[0].path).write_text(
            'Version 2026.7.15\nConstruction of interfragment charger-transfer matrix has finished!\n')
        self.fixture.validation()
        self.snapshot = self.fixture.service.poll(self.fixture.context)
        self.case = {'profile': 'IFCT', 'attempt_id': 'jobs-attempt', 'literal_text': [
            'Two-fragment IFCT', 'singlet', 'State 1', '2 threads', '2 frags',
            'IFCT matrix marker observed', 'Output validation returned successfully',
            'session_log', 'Validator PASSED (recorded)']}

    def test_original_full_literal_predicate_fails_only_width_limited_stage(self):
        # The shared portal card is seven cells narrower than its terminal.
        for width in (133, 93, 70, 73):
            with self.subTest(width=width):
                text = card_text(self.snapshot, width=width, now=0, max_lines=8, framed=True)
                evidence = analysis_painted(self.snapshot, self.case, text.splitlines())
                truncated = any(row['display'] == 'explicit-ellipsis' for row in evidence['stage_rendering'])
                self.assertEqual(truncated, width != 133)
                self.assertEqual(all(token in text for token in self.case['literal_text']), width == 133)
                self.assertEqual(evidence['full_literals'], self.case['literal_text'])

    def test_missing_or_wrong_scope_output_or_stage_never_passes(self):
        text = card_text(self.snapshot, width=93, now=0, max_lines=8, framed=True)
        for old, new in [('singlet', 'triplet'), ('session_log', 'missing_log'),
                         ('PASSED', 'FAILED'), ('Runner:', 'Absent:'),
                         ('Output validation', 'Output invalidation'), ('…', '')]:
            with self.subTest(old=old):
                self.assertIn(old, text)
                with self.assertRaises(AssertionError):
                    analysis_painted(self.snapshot, self.case, text.replace(old, new).splitlines())
        with self.assertRaises(AssertionError):
            analysis_evidence(self.snapshot, {**self.case, 'attempt_id': 'different-attempt'})
        changed = replace(self.snapshot, settings=tuple(
            replace(setting, value='Wrong runner stage') if setting.name == 'runner_stage' else setting
            for setting in self.snapshot.settings))
        with self.assertRaises(AssertionError):
            analysis_painted(changed, self.case, text.splitlines())

    def test_details_requires_complete_stages_and_attempt(self):
        evidence = analysis_evidence(self.snapshot, self.case)
        body = progress_text(self.snapshot, detailed=True)
        result = analysis_details(evidence, body)
        self.assertFalse(result['painted_full_stage_claimed'])
        for required in result['full_literals']:
            with self.subTest(required=required):
                with self.assertRaises(AssertionError):
                    analysis_details(evidence, body.replace(required, 'missing'))
