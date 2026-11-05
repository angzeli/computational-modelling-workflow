"""Cheap mutation-driver contracts; no runtime enrollment or child launch."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from tests.campaign import closure_mutations as driver


class MutationDriverTests(unittest.TestCase):
    def test_every_selector_and_patch_has_one_exact_source_location(self):
        plan = driver.preflight(Path(__file__).resolve().parents[2])
        self.assertEqual(len(plan), 6)
        self.assertEqual(len({case['name'] for case in plan}), 6)
        self.assertTrue(all(case['target_line'] > 0 for case in plan))

    def test_missing_and_duplicated_targets_refuse_before_mutation(self):
        for case in driver.CASES:
            for count in (0, 2):
                with self.subTest(case=case['name'], count=count):
                    with self.assertRaisesRegex(ValueError, f"{case['name']}.*expected exactly one.*found {count}"):
                        driver.apply_mutation(case, '\n'.join([case['before']] * count))

    def test_only_reached_specific_invariant_failure_kills(self):
        case = {'expected': 'EXACT_INVARIANT'}
        baseline = {'returncode': 0, 'result': {'status': 'PASS', 'target_execution': [1]}}
        mutant = {'returncode': 1, 'result': {'status': 'TEST_FAILED', 'target_execution': [1],
                  'failures': [('test', 'AssertionError: EXACT_INVARIANT')], 'errors': [], 'skipped': []}}
        self.assertTrue(driver.mutation_killed(case, baseline, mutant))
        for changes in ({'status': 'HARNESS_FAILED'}, {'target_execution': []},
                        {'errors': [('setup', 'runtime unavailable')]}, {'skipped': [('test', 'unavailable')]},
                        {'failures': [('test', 'AssertionError: sampling unavailable')]}):
            changed = deepcopy(mutant)
            changed['result'].update(changes)
            self.assertFalse(driver.mutation_killed(case, baseline, changed), changes)
        failed_baseline = deepcopy(baseline)
        failed_baseline['result']['status'] = 'HARNESS_FAILED'
        self.assertFalse(driver.mutation_killed(case, failed_baseline, mutant))

    def test_source_copy_is_physical_and_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            for name in ('src', 'tests', 'scripts'):
                (source / name).mkdir()
                (source / name / 'fixture.py').write_text('pass\n')
            for name in ('pyproject.toml', 'README.md'):
                (source / name).write_text('fixture\n')
            copied = root / 'copy'
            driver.copy_source(source, copied)
            self.assertNotEqual((source / 'src/fixture.py').stat().st_ino,
                                (copied / 'src/fixture.py').stat().st_ino)
            (source / 'src/linked.py').symlink_to(source / 'src/fixture.py')
            with self.assertRaisesRegex(AssertionError, 'symlink refused'):
                driver.copy_source(source, root / 'rejected')
