"""Single transient observation faults must not lose an attempt's completion."""
from collections import Counter
import json
import os
from pathlib import Path
import unittest

from tests.jobs.ownership_fault_sweep import (
    FAULTS, OUTCOMES, SweepBoundExceeded, assert_terminal, run_lifecycle, selected_points, summarize)
from tests.jobs.ownership_read_manifest import CONTRACT_TESTS, LIMITS, coverage_manifest, read_sites
from tests.jobs.ownership_sweep_reporting import WALL_SECONDS, RowWatchdog, RowWatchdogExpired, clock, emit


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
        cls.rows, cls.manifest, cls.timings = [], [], []
        cls.completed_rows = 0
        cls.started = clock()

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
                       completed=all(item['completed'] for item in cls.timings), scenario_timings=cls.timings,
                       elapsed_seconds=clock()-cls.started)
        emit('SWEEP', **summary)
        evidence = os.environ.get('CMW_TEST_EVIDENCE_DIR')
        if evidence:
            output = Path(evidence)/'ownership-fault-sweep.json'
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(dict(summary=summary, rows=cls.rows,
                dynamic_manifest=cls.manifest, source_manifest=source_manifest, limits=LIMITS),
                indent=2) + '\n')

    def observe(self, route, outcome, point=None, fault=None, baseline=None):
        identity = dict(route=route, outcome=outcome,
                        point=point.label() if point else '<baseline>', fault=fault)
        started = clock()
        emit('ROW', event='START', **identity, rows_completed=self.completed_rows,
             elapsed_seconds=started-self.started, wall_budget_seconds=WALL_SECONDS)
        result = None
        try:
            with RowWatchdog(identity) as watchdog:
                try:
                    result = run_lifecycle(outcome, route, point, fault, diagnostics=watchdog.capture)
                    if point is not None:
                        self.rows[-1]['injected_count'] = result['fired']
                    outcome_class = assert_terminal(self, result, outcome, baseline)
                except SweepBoundExceeded as exc:
                    watchdog.fail(exc.kind, str(exc), observed=exc.observed, limit=exc.limit)
            return result, outcome_class
        except RowWatchdogExpired:
            raise  # The watchdog already emitted the exact failing row.
        except BaseException as exc:
            emit('ROW', event='FAILED', **identity, error=f'{type(exc).__name__}: {exc}')
            raise
        finally:
            emit('ROW', event='END', **identity, elapsed_seconds=clock()-self.started,
                 row_elapsed_seconds=clock()-started,
                 virtual_elapsed_seconds=result['clock'] if result is not None else None,
                 returned=result is not None)

    def matrix(self, route, outcome):
        started, offset = clock(), len(self.rows)
        completed = False
        emit('SCENARIO', event='START', route=route, outcome=outcome,
             rows_completed=offset, elapsed_seconds=started-self.started)
        try:
            self.run_matrix(route, outcome)
            completed = True
        except RowWatchdogExpired as exc:
            counts = Counter(row['result'] for row in self.rows)
            emit('SWEEP', completed=False, stopped_row=json.loads(str(exc)),
                 attempted_rows=len(self.rows), completed_rows=self.completed_rows,
                 passed_rows=counts['passed'], failed_rows=counts['failed'],
                 elapsed_seconds=clock()-self.started)
            raise
        finally:
            rows = self.rows[offset:]
            counts = Counter(row['result'] for row in rows)
            summary = dict(route=route, outcome=outcome, attempted_rows=len(rows),
                           completed=completed,
                           completed_rows=sum(row.get('completed', False) for row in rows),
                           passed_rows=counts['passed'], failed_rows=counts['failed'],
                           elapsed_seconds=clock()-started)
            self.timings.append(summary)
            emit('SCENARIO', event='END', **summary)

    def run_matrix(self, route, outcome):
        baseline, _ = self.observe(route, outcome)
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
                           result='failed', completed=False)
                self.rows.append(row)
                with self.subTest(route=route, outcome=outcome, point=point.label(), fault=fault):
                    started = clock()
                    try:
                        result, outcome_class = self.observe(route, outcome, point, fault, baseline)
                        row['injected_count'] = result['fired']
                        row['outcome_class'] = outcome_class
                    except RowWatchdogExpired as exc:
                        row['error'] = f'{type(exc).__name__}: {exc}'
                        raise
                    except BaseException as exc:
                        row['error'] = f'{type(exc).__name__}: {exc}'
                        row['completed'] = True
                        type(self).completed_rows += 1
                        raise
                    finally:
                        row['wall_elapsed_seconds'] = clock()-started
                    row['result'] = 'passed'
                    row['completed'] = True
                    type(self).completed_rows += 1


def lifecycle_test(route, outcome):
    def test(self):
        self.matrix(route, outcome)
    test.__doc__ = f'{route}: {outcome}; one fault at each reached read occurrence.'
    return test


for _route in ROUTES:
    for _outcome in OUTCOMES:
        setattr(OwnershipFaultSweepTests, f'test_{_route}_{_outcome}',
                lifecycle_test(_route, _outcome))
