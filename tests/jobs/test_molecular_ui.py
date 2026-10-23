from pathlib import Path
import tempfile
import unittest

try:
    import textual
except ImportError:
    textual = None


@unittest.skipIf(textual is None, 'Optional Jobs console is not installed')
class MolecularUiTests(unittest.TestCase):
    def test_existing_frame_four_viewports_and_controls(self):
        from tests.jobs.molecular_ui_acceptance import molecular_ui_surface
        with tempfile.TemporaryDirectory(prefix='cmw-molecular-ui-') as temporary:
            report = molecular_ui_surface(Path(temporary)/'fixtures')
        self.assertEqual(len(report['views']), 24)
        self.assertEqual(len(report['controls']), 6)
        self.assertTrue(report['queue_unchanged'])
        self.assertEqual(report['issues'], [])
