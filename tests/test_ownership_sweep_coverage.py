"""Coverage proof rejects partial, repeated and unsuccessful injected rows."""
import copy
import unittest

from scripts.ci.check_ownership_sweep import verify
from tests.jobs.ownership_fault_sweep import FAULTS, OUTCOMES, ReadPoint
from tests.jobs.test_ownership_fault_sweep import ROUTES


class SweepCoverageTests(unittest.TestCase):
    def setUp(self):
        point = dict(module='fixture', function='read', line=1, api='read', occurrence=1)
        manifest = [dict(route=route, outcome=outcome, baseline_reads=[point], selected=[point])
                    for route in ROUTES for outcome in OUTCOMES]
        rows = [dict(route=item['route'], outcome=item['outcome'], point=ReadPoint(**point).label(),
                     fault=fault, result='passed', completed=True, injected_count=1, outcome_class='terminal')
                for item in manifest for fault in FAULTS]
        self.evidence = dict(summary=dict(mode='full', completed=True, attempted_rows=len(rows),
            passed_rows=len(rows), failed_rows=0, baseline_lifecycles=len(manifest),
            terminal_rows=len(rows), pending_never_observed_domain_rows=0),
            dynamic_manifest=manifest, rows=rows)

    def test_unsharded_union_is_exact_and_accepts_both_documented_outcomes(self):
        self.evidence['rows'][0]['outcome_class'] = 'pending_never_observed_domain'
        self.evidence['summary']['terminal_rows'] -= 1
        self.evidence['summary']['pending_never_observed_domain_rows'] = 1
        self.assertEqual(verify(self.evidence), dict(scenarios=15, expected_rows=45,
            actual_rows=45, missing=0, extra=0, duplicates=0, terminal=44,
            pending_never_observed_domain=1))

    def test_missing_duplicate_extra_or_filtered_row_is_rejected(self):
        for change in ('missing', 'duplicate', 'extra', 'filtered', 'scenario'):
            with self.subTest(change=change):
                value = copy.deepcopy(self.evidence)
                if change == 'missing':
                    value['rows'].pop()
                elif change == 'duplicate':
                    value['rows'].append(value['rows'][0])
                elif change == 'extra':
                    value['rows'][0]['fault'] = 'other'
                elif change == 'filtered':
                    value['dynamic_manifest'][0]['selected'] = []
                else:
                    value['dynamic_manifest'][-1] = value['dynamic_manifest'][0]
                with self.assertRaises(ValueError):
                    verify(value)

    def test_incomplete_failed_or_inconsistent_evidence_is_rejected(self):
        for key, replacement in (('result', 'failed'), ('completed', False), ('injected_count', 0)):
            with self.subTest(key=key):
                value = copy.deepcopy(self.evidence)
                value['rows'][0][key] = replacement
                with self.assertRaises(ValueError):
                    verify(value)
        for key, replacement in (('mode', 'representative'), ('completed', False), ('passed_rows', 0)):
            with self.subTest(key=key):
                value = copy.deepcopy(self.evidence)
                value['summary'][key] = replacement
                with self.assertRaises(ValueError):
                    verify(value)
