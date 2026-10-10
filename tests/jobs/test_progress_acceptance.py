"""Shared read-only acceptance also executed against the installed wheel."""
from pathlib import Path
import tempfile
import unittest

from tests.jobs.progress_acceptance import progress_surface

try:
    import textual
except ImportError:
    textual = None


class ProgressAcceptanceTests(unittest.TestCase):
    def test_read_only_cli_all_registered_engines_and_files_open_boundary(self):
        with tempfile.TemporaryDirectory(prefix='cmw-progress-acceptance-') as temporary:
            result = progress_surface(Path(temporary)/'cli')
        self.assertTrue(result['source_and_state_unchanged'])
        self.assertEqual(set(result['cli']), {'VASP', 'ORCA', 'FICTIONAL-EXTENSION', 'UNKNOWN-EXTENSION'})

    @unittest.skipIf(textual is None, 'Optional Jobs console not installed')
    def test_four_engine_cards_history_note_files_and_detach(self):
        with tempfile.TemporaryDirectory(prefix='cmw-progress-console-') as temporary:
            result = progress_surface(Path(temporary)/'console', console=True)
        self.assertEqual(result['console']['reader_contexts_after_close'], 0)
