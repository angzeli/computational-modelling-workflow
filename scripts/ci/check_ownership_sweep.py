"""Prove the dedicated full sweep equals its unfiltered baseline row set."""
from collections import Counter
import json
from pathlib import Path
import sys


def verify(evidence):
    # Derive the expected cross-product from the same public scenario constants
    # and every recorded baseline occurrence, never from the selected rows.
    from tests.jobs.ownership_fault_sweep import FAULTS, OUTCOMES, ReadPoint
    from tests.jobs.test_ownership_fault_sweep import ROUTES

    summary = evidence['summary']
    if summary['mode'] != 'full' or not summary['completed']:
        raise ValueError('Coverage requires a completed full matrix')
    scenarios = [(item['route'], item['outcome']) for item in evidence['dynamic_manifest']]
    expected_scenarios = {(route, outcome) for route in ROUTES for outcome in OUTCOMES}
    if len(scenarios) != len(expected_scenarios) or set(scenarios) != expected_scenarios:
        raise ValueError('Missing or duplicate baseline scenario')
    expected = Counter()
    for item in evidence['dynamic_manifest']:
        if not item['baseline_reads'] or item['selected'] != item['baseline_reads']:
            raise ValueError('Full matrix filtered its baseline occurrences')
        for point in item['baseline_reads']:
            for fault in FAULTS:
                expected[(item['route'], item['outcome'], ReadPoint(**point).label(), fault)] += 1
    actual = Counter((row['route'], row['outcome'], row['point'], row['fault'])
                     for row in evidence['rows'])
    if any(count != 1 for count in expected.values()) or actual != expected:
        raise ValueError(f'Row union differs: missing={sum((expected-actual).values())}, '
                         f'extra={sum((actual-expected).values())}')
    if any(row['result'] != 'passed' or not row['completed'] or row['injected_count'] != 1
           for row in evidence['rows']):
        raise ValueError('A row failed, was incomplete, or did not inject exactly once')
    counts = Counter(row['outcome_class'] for row in evidence['rows'])
    if (summary['attempted_rows'] != len(actual) or summary['passed_rows'] != len(actual)
            or summary['failed_rows'] != 0 or summary['baseline_lifecycles'] != len(scenarios)
            or summary['terminal_rows'] != counts['terminal']
            or summary['pending_never_observed_domain_rows'] != counts['pending_never_observed_domain']
            or sum(counts.values()) != counts['terminal'] + counts['pending_never_observed_domain']):
        raise ValueError('Summary does not account for every row and outcome')
    return dict(scenarios=len(scenarios), expected_rows=len(expected), actual_rows=len(actual),
                missing=0, extra=0, duplicates=0, terminal=counts['terminal'],
                pending_never_observed_domain=counts['pending_never_observed_domain'])


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    print('OWNERSHIP_FAULT_UNION ' + json.dumps(verify(json.loads(Path(sys.argv[1]).read_text()))))
