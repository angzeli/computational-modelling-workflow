"""One real endpoint/worker workflow; no engines or production processes."""
import json
import os
from pathlib import Path
from uuid import uuid4
import subprocess
import tempfile
import unittest

from cmw.jobs.ownership import identity
from cmw.nodes.storage import platform_volume
from tests.nodes.acceptance import _outstanding_owners, node_surface


class NodeWorkflowTests(unittest.TestCase):
    def test_cleanup_requires_positive_absence(self):
        live = identity()
        reused = dict(live, start_token=live['start_token'] + 1)
        unreadable = dict(live, host='another-fixture-host')
        outstanding = _outstanding_owners([live, reused, unreadable])
        self.assertEqual(outstanding, [
            {'owner': live, 'status': 'VERIFIED', 'deciding_check': 'identity'},
            {'owner': unreadable, 'status': 'UNVERIFIABLE', 'deciding_check': 'host'}])

    def test_owned_worker_survives_endpoint_and_completed_result_fetches(self):
        with tempfile.TemporaryDirectory(prefix='cmw-node-workflow-') as directory:
            root = Path(directory).resolve()
            try:
                platform_volume(root)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                self.skipTest(f'Actual local-volume identity unavailable: {exc}')
            report = node_surface(root / 'node-workflow')
            cleanup = json.loads((root / 'node-workflow/cleanup.json').read_text())
            self.assertTrue(report['payload_owners'])
            self.assertEqual(cleanup['remaining_verified_owners'], [])
            self.assertEqual(cleanup['unverifiable_owners'], [])
            evidence = os.environ.get('CMW_TEST_EVIDENCE_DIR')
            if evidence:
                destination = Path(evidence)
                destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                report['cleanup'] = cleanup
                (destination / f'node-workflow-proof-{uuid4().hex}.json').write_text(json.dumps(report, indent=2) + '\n')
            self.assertTrue(all(report['checks'].values()))
            self.assertFalse(report['scientific_engine_executed'])
