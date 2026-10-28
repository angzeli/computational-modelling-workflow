"""Focused new-contract checks with only the selected installed production code."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import sys
import unittest

import cmw
from cmw.nodes.identity import runtime_identity


MODULES = [
    'tests.nodes.test_runtime_observation', 'tests.progress.test_vasp_setting_context',
    'tests.campaign.test_observation', 'tests.campaign.test_catalogue_writer',
    'tests.progress.test_generated_schedules', 'tests.progress.test_multiwfn',
    'tests.jobs.test_research_invariants',
    'tests.nodes.test_integrated_portal', 'tests.jobs.test_integrated_navigation',
    'tests.doctor.test_service', 'tests.doctor.test_capsule', 'tests.nodes.test_doctor',
    'tests.jobs.test_doctor_ui', 'tests.jobs.test_review_service', 'tests.jobs.test_review_history',
    'tests.jobs.test_catalogue_store', 'tests.nodes.test_catalogue',
    'tests.jobs.test_successor', 'tests.jobs.test_study_plan',
    'tests.jobs.test_study_comparison', 'tests.jobs.test_study', 'tests.nodes.test_study',
]


def main():
    kind, output = sys.argv[1], Path(sys.argv[2])
    assert kind in {'core', 'jobs'}
    assert Path(cmw.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), cmw.__file__
    assert not (Path(__file__).resolve().parents[2]/'src').exists()
    assert (importlib.util.find_spec('textual') is not None) == (kind == 'jobs')
    modules = MODULES + (['tests.campaign.test_geometry', 'tests.nodes.test_portal_navigation'] if kind == 'jobs' else [])
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(modules))
    report = {'kind': kind, 'python': sys.executable, 'package_origin': cmw.__file__,
        'runtime': runtime_identity(), 'modules': modules, 'tests': result.testsRun, 'failures': len(result.failures),
        'errors': len(result.errors), 'skips': len(result.skipped),
        'successful': result.wasSuccessful() and (kind != 'jobs' or not result.skipped)}
    output.write_text(json.dumps(report, indent=2)+'\n')
    raise SystemExit(not report['successful'])


if __name__ == '__main__':
    main()
