"""Small fixture count is explicit; native case and actual rows stay separate."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.jobs.store import Store
from tests.campaign.support import fixture


class CompactFixtureTests(unittest.TestCase):
    def test_explicit_small_history_reuses_native_fixture_and_preserves_case_label(self):
        with tempfile.TemporaryDirectory(prefix='cmw-compact-fixture-') as temporary:
            config = {'node_id':'synthetic-node','host_id':'synthetic-host',
                      'runtime':{'fingerprint':'synthetic-runtime'},'queues':{'research':{'queue_id':'synthetic-queue'}}}
            with patch('cmw.nodes.identity.enroll',return_value=config):
                record = fixture(Path(temporary)/'relaxation',10000,history_rows=3)
            jobs = Store(record['state']).snapshot()['jobs']
            self.assertEqual(len(jobs),3)
            self.assertEqual(record['rows'],10000)
            self.assertEqual(record['actual_history_rows'],3)
            self.assertTrue(all(job['status']=='Hold' for job in jobs))
            self.assertIn('progress_sources',jobs[0])
            self.assertNotIn('progress_sources',jobs[1])

    def test_invalid_small_history_is_refused_before_creating_files(self):
        with tempfile.TemporaryDirectory(prefix='cmw-compact-fixture-') as temporary:
            root=Path(temporary)/'invalid'
            with self.assertRaises(ValueError):
                fixture(root,1000,history_rows=1)
            self.assertFalse(root.exists())
