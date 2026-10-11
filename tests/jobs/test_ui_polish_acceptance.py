"""Viewport and interaction acceptance, shared with installed-wheel smoke."""
from pathlib import Path
import tempfile
import unittest

try:
    import textual
except ImportError:
    textual = None


@unittest.skipIf(textual is None, 'Optional Jobs console not installed')
class UiPolishAcceptanceTests(unittest.TestCase):
    def test_synthetic_native_cards_warnings_and_controls_at_four_sizes(self):
        from tests.jobs.ui_polish_acceptance import SCENARIOS, SIZES, ui_polish_surface
        with tempfile.TemporaryDirectory(prefix='cmw-ui-polish-') as temporary:
            report = ui_polish_surface(Path(temporary) / 'fixtures')
        self.assertEqual(len(report['views']), len(SCENARIOS) * len(SIZES))
        self.assertEqual(report['controls'], list(SCENARIOS))
        self.assertTrue(report['source_and_state_unchanged'])
        self.assertEqual(report['reader_contexts_after_close'], 0)
        self.assertEqual(report['issues'], [])
