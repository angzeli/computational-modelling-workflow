"""Catalogue paging with one real benign controller/worker heartbeat writer."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from cmw.jobs import runtime
from cmw.jobs.catalogue import CatalogueStore
from cmw.jobs.catalogue_models import CatalogueQuery
from cmw.jobs.store import Store
from tests.campaign.support import ProcessLedger
from tests.jobs.isolated_runtime import install
from tests.jobs.test_catalogue_store import populate_history
from tests.jobs.test_jobs import wait_for


class ActiveWriterTests(unittest.TestCase):
    def test_heartbeat_preserves_pages_and_exact_attempts(self):
        evidence = {'scenario': self.id(), 'events': []}
        with tempfile.TemporaryDirectory(prefix='cmw-catalogue-writer-') as directory:
            root = Path(directory).resolve()
            store = Store(root/'state')
            gate = root/'finish'
            code = "from pathlib import Path; import time\ngate=Path(" + repr(str(gate)) + ")\nwhile not gate.exists(): time.sleep(.05)"
            job = store.add(argv=[sys.executable, '-I', '-B', '-c', code], cwd=root,
                            name='benign active writer', hold=True)
            populate_history(store, root, 120, first=job)
            store.change(job['id'], 'release')
            ledger = ProcessLedger(self.id())
            with ledger, install():
                try:
                    runtime.start(store)
                    wait_for(lambda: store.snapshot()['jobs'][0]['status'] == 'Run')
                    catalogue = CatalogueStore(store)
                    query = CatalogueQuery(limit=50)
                    page = catalogue.query(query)
                    heartbeat = store.snapshot()['controller']['heartbeat']
                    ledger.sample()
                    later = wait_for(lambda: (snapshot if snapshot['controller']['heartbeat'] > heartbeat else None)
                                     if (snapshot := store.snapshot()) else None)
                    evidence['events'].append({'before_heartbeat': heartbeat,
                        'after_heartbeat': later['controller']['heartbeat'], 'first_generation': page.generation,
                        'first_targets': [(row.job_id, row.attempt_id) for row in page.records]})
                    second = catalogue.query(replace(query, cursor=page.next_cursor))
                    third = catalogue.query(replace(query, cursor=second.next_cursor))
                    all_rows = [*page.records, *second.records, *third.records]
                    self.assertEqual([row.job_id for row in all_rows], list(range(1, 121)))
                    self.assertEqual(len({(row.job_id, row.attempt_id) for row in all_rows}), 120)
                    self.assertIsNone(third.next_cursor)
                    self.assertEqual(page.generation, second.generation)
                    self.assertEqual(store.snapshot()['jobs'][0]['attempt_id'], job['attempt_id'])
                except BaseException as exc:
                    evidence['error'] = repr(exc)
                    raise
                finally:
                    gate.touch()
                    wait_for(lambda: store.snapshot()['jobs'][0]['status'] in {'Done', 'Fail', 'Unknown'}, timeout=10)
                    controller = store.snapshot()['controller']['owner']
                    runtime.stop(store)
                    wait_for(lambda: not store.snapshot()['controller']['online'], timeout=10)
                    for child in tuple(runtime._CHILDREN):
                        if controller and child.pid == controller['pid']:
                            child.wait(timeout=5)
                    runtime.reap_detached()
                    evidence['remaining'] = ledger.cleanup(time.monotonic()+5)
                    evidence['ownership'] = ledger.report()
                    if destination := os.environ.get('CMW_TEST_EVIDENCE_DIR'):
                        target = Path(destination); target.mkdir(parents=True, exist_ok=True)
                        (target/'active-writer.json').write_text(json.dumps(evidence, indent=2)+'\n')
                    self.assertFalse(evidence['remaining'])
