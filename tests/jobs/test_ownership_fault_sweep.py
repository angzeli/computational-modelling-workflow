"""Single transient observation faults must not lose an attempt's completion."""
from collections import Counter
import json
import os
from pathlib import Path
import time
import unittest

from tests.jobs.ownership_fault_sweep import (
    FAULTS, OUTCOMES, assert_terminal, run_lifecycle, selected_points, summarize)
from tests.jobs.ownership_read_manifest import CONTRACT_TESTS, LIMITS, coverage_manifest, read_sites


ROUTES = ('group', 'session', 'legacy_session', 'controlled', 'orca')


class ReadManifestTests(unittest.TestCase):
    def test_every_lexical_read_has_an_explicit_applicability_family(self):
        rows = read_sites()
        self.assertEqual({row['module'] for row in rows}, set(CONTRACT_TESTS))
        self.assertTrue(all(row['classification'] in {'worker', 'contract', 'effect', 'recovery'} for row in rows))
        root = Path(__file__).resolve().parents[2]
        for path in {path for paths in CONTRACT_TESTS.values() for path in paths}:
            self.assertTrue((root/path).is_file(), path)


class OwnershipFaultSweepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.representative = os.environ.get('CMW_FAULT_SWEEP') == 'representative'
        cls.rows, cls.manifest = [], []
        cls.started = time.perf_counter()

    @classmethod
    def tearDownClass(cls):
        counts = Counter(row['result'] for row in cls.rows)
        outcomes = Counter(row.get('outcome_class') for row in cls.rows if row['result'] == 'passed')
        source_manifest = coverage_manifest(cls.manifest)
        summary = dict(mode='representative' if cls.representative else 'full',
                       attempted_rows=len(cls.rows), passed_rows=counts['passed'],
                       failed_rows=counts['failed'], baseline_lifecycles=len(cls.manifest),
                       terminal_rows=outcomes['terminal'],
                       pending_never_observed_domain_rows=outcomes['pending_never_observed_domain'],
                       reached_source_reads=sum(row['reached_in_fault_free_baseline'] for row in source_manifest),
                       inventoried_source_calls=len(source_manifest),
                       elapsed_seconds=time.perf_counter()-cls.started)
        print('OWNERSHIP_FAULT_SWEEP ' + json.dumps(summary), flush=True)
        evidence = os.environ.get('CMW_TEST_EVIDENCE_DIR')
        if evidence:
            output = Path(evidence)/'ownership-fault-sweep.json'
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(dict(summary=summary, rows=cls.rows,
                dynamic_manifest=cls.manifest, source_manifest=source_manifest, limits=LIMITS),
                indent=2) + '\n')

    def matrix(self, route, outcome):
        baseline = run_lifecycle(outcome, route)
        assert_terminal(self, baseline, outcome)
        if route in {'controlled', 'orca'}:
            self.assertTrue({'process_identity._parent', 'process_identity._children'} <=
                            {point.api for point in baseline['points']},
                            'native ancestry reads must be reached, not hidden behind a fixture wrapper')
        points = selected_points(baseline['points'], self.representative)
        self.manifest.append(dict(route=route, outcome=outcome,
                                  baseline_reads=summarize(baseline['points']),
                                  selected=summarize(points)))
        for point in points:
            for fault in FAULTS:
                row = dict(route=route, outcome=outcome, point=point.label(), fault=fault,
                           result='failed')
                self.rows.append(row)
                with self.subTest(route=route, outcome=outcome, point=point.label(), fault=fault):
                    try:
                        result = run_lifecycle(outcome, route, point, fault)
                        row['injected_count'] = result['fired']
                        row['outcome_class'] = assert_terminal(self, result, outcome, baseline)
                    except BaseException as exc:
                        row['error'] = f'{type(exc).__name__}: {exc}'
                        raise
                    row['result'] = 'passed'


def lifecycle_test(route, outcome):
    def test(self):
        self.matrix(route, outcome)
    test.__doc__ = f'{route}: {outcome}; one fault at each reached read occurrence.'
    return test


for _route in ROUTES:
    for _outcome in OUTCOMES:
        setattr(OwnershipFaultSweepTests, f'test_{_route}_{_outcome}',
                lifecycle_test(_route, _outcome))
