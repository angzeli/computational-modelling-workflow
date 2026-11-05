"""Six sequential mutation controls; 150s work plus at most 30s cleanup.

Never imports a product from the integration checkout. Every baseline and mutant
runs from its own physical source/test copy. This is mutation evidence, not an
installed-artifact qualification. No control runs in --preflight mode.
"""
from __future__ import annotations
import argparse
import ast
import difflib
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import traceback
import unittest
from uuid import uuid4

HERE = Path(__file__).resolve().parent
DRIVER = Path(__file__).resolve()
HISTORICAL = '/Volumes/Scratch/cmw/CMW-integrated-correctness-2026-10-26'
CASES = [
    dict(name='wrong-attempt-review', file='src/cmw/jobs/review.py',
         before="        if expected_attempt_id is not None and job['attempt_id'] != expected_attempt_id:",
         after='        if False:  # mutation: accept a foreign selected review attempt',
         test='tests.campaign.mutation_oracles.WrongAttemptReviewTests.test_selected_attempt_is_required_before_review',
         test_file='tests/campaign/mutation_oracles.py', expected='JobsError not raised',
         invariant='Review requires the exact selected immutable attempt before inspection.'),
    dict(name='approved-successor-digest', file='src/cmw/jobs/successor.py',
         before="        if plan['status'] != 'READY' or digest != approved_digest or plan['spec'] != spec:",
         after="        if plan['status'] != 'READY' or plan['spec'] != spec:",
         test='tests.campaign.mutation_oracles.SuccessorApprovalTests.test_changed_source_refuses_frozen_approval_before_publication',
         test_file='tests/campaign/mutation_oracles.py', expected='SUCCESSOR_SOURCE_APPROVAL: changed source accepted the frozen reviewed digest',
         invariant='Changed native source invalidates the approved plan before publication.'),
    dict(name='atomic-request-association', file='src/cmw/jobs/store.py',
         before="                self.record_request(con, remote_request['request_id'], 'submit', remote_request['digest'], job)",
         after='                pass  # mutation: omit the atomic durable request association',
         test='tests.campaign.mutation_oracles.DurableAssociationTests.test_durable_request_transaction_boundary',
         test_file='tests/campaign/mutation_oracles.py', expected='ATOMIC_REQUEST_ASSOCIATION: receipt absent inside job transaction',
         invariant='Held registration atomically retains its exact durable request outcome.'),
    dict(name='sp-frame', file='src/cmw/progress/formatting.py',
         before='if unresolved and not snapshot.metrics and not snapshot.card_groups:',
         after='if unresolved and not snapshot.metrics:',
         test='tests.progress.test_generated_schedules.GeneratedProgressScheduleTests.test_electronic_reset_to_empty_preserves_the_scientific_frame',
         test_file='tests/progress/test_generated_schedules.py', expected='AssertionError',
         invariant='Incomplete declared VASP evidence retains the bordered scientific frame.'),
    dict(name='native-frontier', file='src/cmw/progress/vasp.py',
         before="electronic = evaluation['rows'][-1]['label'] if evaluation['rows'] else None",
         after="electronic = next((check.checked_frontier for check in verification if check.domain == 'electronic-row'), None)",
         test='tests.progress.test_generated_schedules.GeneratedProgressScheduleTests.test_open_native_header_keeps_current_context_separate_from_checked_frontier',
         test_file='tests/progress/test_generated_schedules.py', expected='AssertionError',
         invariant='Current native SCF context remains separate from the older checked frontier.'),
    dict(name='viewport-restoration', file='src/cmw/jobs/navigation.py',
         before='    def restore_view(self, snapshot):\n',
         after='    def restore_view(self, snapshot):\n        return  # mutation: discard captured viewport\n',
         test='tests.jobs.test_navigation_ui.NavigationUiTests.test_append_and_reorder_resolve_immutable_target_and_visible_anchor',
         test_file='tests/jobs/test_navigation_ui.py', expected='AssertionError', line_offset=1,
         invariant='Returning after reorder preserves the visible anchor and immutable target.'),
]


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def apply_mutation(case, source):
    count = source.count(case['before'])
    if count != 1:
        raise ValueError(f"{case['name']} in {case['file']}: expected exactly one mutation target, found {count}")
    return source.replace(case['before'], case['after'], 1)


def preflight(checkout):
    results = []
    for case in CASES:
        path = checkout / case['file']
        source = path.read_text()
        ast.parse(apply_mutation(case, source))
        test = checkout / case['test_file']
        tree = ast.parse(test.read_text())
        class_name, method = case['test'].split('.')[-2:]
        cls, = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name]
        assert any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method for node in cls.body)
        results.append({**case, 'target_line': source[:source.index(case['before'])].count('\n') + 1 + case.get('line_offset', 0),
                        'source_sha256': digest(path), 'test_sha256': digest(test)})
    return results


def copy_source(checkout, destination):
    destination.mkdir(mode=0o700)
    def ignore(directory, names):
        excluded = {name for name in names if name == '__pycache__' or name.endswith('.pyc')}
        for name in set(names) - excluded:
            assert not (Path(directory) / name).is_symlink(), ('source symlink refused', directory, name)
        return excluded
    for name in ('src', 'tests', 'scripts'):
        shutil.copytree(checkout / name, destination / name, ignore=ignore, copy_function=shutil.copy2)
    for name in ('pyproject.toml', 'README.md'):
        shutil.copy2(checkout / name, destination / name)


def import_copy(copy):
    sys.path[:0] = [str(copy / 'src'), str(copy)]
    import cmw
    expected = copy / 'src/cmw/__init__.py'
    assert Path(cmw.__file__).resolve() == expected, cmw.__file__
    return {'python': sys.executable, 'package': str(expected), 'copy': str(copy),
            'qualification': 'physical source mutation control, not installed acceptance'}


def configure_runtime(copy, ledgers):
    """Keep the existing fixture-only activity boundary and add launch evidence."""
    from tests.jobs import isolated_runtime
    from cmw.jobs import runtime
    from cmw.jobs.store import private_opener
    def detached(arguments, log):
        log = Path(log); log.parent.mkdir(parents=True, exist_ok=True)
        runtime.reap_detached()
        evidence = ledgers / ('runtime-' + uuid4().hex + '.json')
        command = [sys.executable, '-I', '-B', str(DRIVER), '--runtime-child',
                   str(copy), str(evidence), *arguments]
        with open(log, 'ab', buffering=0, opener=private_opener) as output:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                stderr=output, start_new_session=True, close_fds=True, cwd='/',
                env={key: value for key, value in os.environ.items() if key != 'PYTHONPATH'})
        runtime._CHILDREN.append(process)
        return process
    isolated_runtime.detached = detached
    return isolated_runtime


def child():
    kind, copy, result_path = sys.argv[1], Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve()
    origin = import_copy(copy)
    from tests.campaign.closure_workflows import monitored
    if kind == '--runtime-child':
        save(result_path.with_suffix('.origin.json'), origin)
        launcher = configure_runtime(copy, result_path.parent)
        sys.argv = [sys.argv[0], *sys.argv[4:]]
        with monitored('mutation-owned-runtime', result_path):
            launcher.main()
        return
    selector, module_name, expected_sha, ledgers, target_line = sys.argv[4:9]
    configure_runtime(copy, Path(ledgers))
    report = {'origin': origin, 'test': selector, 'status': 'RUNNING'}
    code = 2
    try:
        module = importlib.import_module(module_name)
        target = Path(module.__file__).resolve()
        assert target.is_relative_to(copy / 'src') and digest(target) == expected_sha
        test_module = importlib.import_module(selector.rsplit('.', 2)[0])
        test_path = Path(test_module.__file__).resolve()
        assert test_path.is_relative_to(copy)
        report['identities'] = {'module': str(target), 'module_sha256': digest(target),
                                'test': str(test_path), 'test_sha256': digest(test_path)}
        execution = []
        def trace_target(frame, event, _argument):
            if (event == 'line' and frame.f_code.co_filename == str(target)
                    and frame.f_lineno == int(target_line)):
                execution.append({'function': frame.f_code.co_name, 'line': frame.f_lineno})
            return trace_target
        with monitored('mutation-test:' + selector, result_path.parent / 'ownership.json'):
            sys.settrace(trace_target)
            suite = unittest.defaultTestLoader.loadTestsFromName(selector)
            assert suite.countTestCases() == 1, 'Exactly one selected baseline/mutant test is required'
            try:
                result = unittest.TextTestRunner(verbosity=2, failfast=True).run(suite)
            finally:
                sys.settrace(None)
                report['target_execution'] = execution
            report.update(functional_result='PASS' if result.wasSuccessful() else 'FAIL',
                          test_exit_code=0 if result.wasSuccessful() else 1, tests_run=result.testsRun, failures=[(str(test), detail) for test, detail in result.failures],
                          errors=[(str(test), detail) for test, detail in result.errors],
                          skipped=[(str(test), why) for test, why in result.skipped])
        assert result.testsRun == 1 and not result.skipped
        assert execution, 'Patched line was not executed by the selected test'
        report['status'] = 'PASS' if result.wasSuccessful() else 'TEST_FAILED'
        code = 0 if result.wasSuccessful() else 1
    except BaseException as exc:
        report.update(status='HARNESS_FAILED', harness_exit_code=2, error={'type': type(exc).__name__, 'message': str(exc),
                                                     'traceback': traceback.format_exc()})
    report['child_exit_code'] = code
    save(result_path, report)
    raise SystemExit(code)


def mutation_killed(case, baseline, mutant):
    """Only the reached invariant assertion kills; a monitor/error never does."""
    original, changed = baseline['result'], mutant['result']
    return bool(baseline['returncode'] == 0 and original['status'] == 'PASS'
        and original.get('target_execution') and mutant['returncode'] == 1
        and changed['status'] == 'TEST_FAILED' and changed.get('target_execution')
        and changed.get('failures') and not changed.get('errors') and not changed.get('skipped')
        and case['expected'] in '\n'.join(detail for _, detail in changed['failures']))


def execute_test(copy, case, phase, output, python, work_deadline, ledgers):
    destination = output / phase; destination.mkdir(mode=0o700)
    module = case['file'].removeprefix('src/').removesuffix('.py').replace('/', '.')
    command = [str(python), '-I', '-B', str(DRIVER), '--test-child', str(copy),
               str(destination / 'test.json'), case['test'], module, digest(copy / case['file']), str(ledgers), str(case['target_line'])]
    timeout = min(35., work_deadline - time.monotonic())
    assert timeout > 0, 'Declared mutation work budget exhausted'
    environment = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
    environment.update(PYTHONDONTWRITEBYTECODE='1', CMW_RESEARCH_TRACE_DIR=str(destination / 'trace'))
    started = time.monotonic()
    with (destination / 'test.log').open('wb') as log:
        process = subprocess.Popen(command, cwd=copy, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, close_fds=True)
        code = process.wait(timeout=timeout)
    result = json.loads((destination / 'test.json').read_text())
    return {'command': command, 'cwd': str(copy), 'seconds': time.monotonic() - started,
            'timeout_seconds': timeout, 'returncode': code, 'result': result,
            'log': str(destination / 'test.log'), 'report': str(destination / 'test.json')}


def git(checkout, *arguments):
    return subprocess.check_output(['git', '-C', str(checkout), *arguments], text=True, timeout=10).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--expected-commit')
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--case', action='append', choices=[case['name'] for case in CASES],
                        help='Bounded development subset; omit for all six qualification controls')
    args = parser.parse_args()
    checkout, output = args.checkout.resolve(), args.output.resolve()
    assert not output.exists(), 'Refusing to overwrite mutation evidence'
    assert not output.is_relative_to(checkout) and not checkout.is_relative_to(output), 'Evidence must be external to source'
    output.mkdir(parents=True, mode=0o700)
    plan = preflight(checkout)
    if args.case:
        plan = [case for case in plan if case['name'] in args.case]
    if args.preflight:
        save(output / 'plan.json', {'status': 'PREFLIGHT_ONLY', 'cases': plan, 'tests_executed': 0,
            'historical_basis': [HISTORICAL + '/counterexamples/progress/run_mutations.py',
                HISTORICAL + '/evidence/mutations-research/summary.json',
                HISTORICAL + '/artifacts/mutations/viewport/run_mutation.py'],
            'difference': 'Wrong-attempt control targets the review guard; historical control covered hold/release.'})
        return
    assert args.expected_commit, 'A frozen exact commit is required'
    started = time.monotonic(); work_deadline = started + 150
    report = {'status': 'RUNNING', 'qualification': 'physical source mutation controls only',
              'required_controls': [case['name'] for case in plan], 'complete_six_control_run': not args.case, 'wall_budget_seconds': 180, 'work_budget_seconds': 150, 'cleanup_budget_seconds': 30,
              'checkout': str(checkout), 'expected_commit': args.expected_commit, 'cases': [],
              'driver_sha256': digest(Path(__file__)), 'oracle_sha256': digest(HERE / 'mutation_oracles.py')}
    original_alarm = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError('150s mutation work budget exhausted')))
    signal.alarm(150)
    try:
        template = output / 'physical-template'; copy_source(checkout, template)
        report['origin'] = import_copy(template)
        from tests.campaign.closure_workflows import monitored
        ledgers = output / 'ledgers'; ledgers.mkdir(mode=0o700)
        with monitored('mutation-controls', ledgers / 'driver.json') as ledger:
            report['actual_commit'] = git(checkout, 'rev-parse', 'HEAD')
            assert report['actual_commit'] == args.expected_commit
            assert not git(checkout, 'status', '--porcelain', '--untracked-files=no'), 'Tracked source is dirty'
            for case in plan:
                ledger.phase = case['name']
                result = {'name': case['name'], 'status': 'RUNNING', 'plan': case}
                report['cases'].append(result)
                destination = output / case['name']; destination.mkdir(mode=0o700)
                copy = destination / 'physical-source'; copy_source(checkout, copy)
                target = copy / case['file']; source = target.read_text()
                assert digest(target) == case['source_sha256']
                original = (checkout / case['file']).stat(); duplicate = target.stat()
                assert (original.st_dev, original.st_ino) != (duplicate.st_dev, duplicate.st_ino), 'Hard-linked source refused'
                result['physical_source'] = {'path': str(copy), 'original_inode': original.st_ino,
                                            'copy_inode': duplicate.st_ino, 'device': duplicate.st_dev}
                baseline = execute_test(copy, case, 'baseline', destination, args.python.absolute(), work_deadline, ledgers)
                result['baseline'] = baseline
                if baseline['returncode'] != 0 or baseline['result']['status'] != 'PASS':
                    result['status'] = 'BASELINE_FAILED'; save(destination / 'result.json', result)
                    continue
                changed = apply_mutation(case, target.read_text())
                target.write_text(changed)
                patch = destination / 'mutation.patch'
                patch.write_text(''.join(difflib.unified_diff(source.splitlines(keepends=True), changed.splitlines(keepends=True),
                                                           fromfile=case['file'], tofile=case['file'])))
                result.update(patch=str(patch), patch_sha256=digest(patch), mutant_sha256=digest(target))
                mutant = execute_test(copy, case, 'mutant', destination, args.python.absolute(), work_deadline, ledgers)
                result['mutant'] = mutant
                test = mutant['result']
                killed = mutation_killed(case, baseline, mutant)
                result['status'] = 'KILLED' if killed else 'NOT_KILLED'
                save(destination / 'result.json', result)
                assert digest(checkout / case['file']) == case['source_sha256'], 'Integration source changed'
            assert git(checkout, 'rev-parse', 'HEAD') == args.expected_commit
            assert not git(checkout, 'status', '--porcelain', '--untracked-files=no')
        paths = [ledgers / 'driver.json', *ledgers.glob('runtime-*.json')]
        paths = [path for path in paths if not path.name.endswith('.origin.json')]
        paths += list(output.glob('*/baseline/ownership.json')) + list(output.glob('*/mutant/ownership.json'))
        report['ledger_audit'] = []
        for path in paths:
            owned = json.loads(path.read_text())
            from tests.campaign.qualification_v2 import outcomes
            outcome = outcomes(functional_passed=True,
                safety_passed=owned['control_identity_safety'] == 'PASS',
                cleanup_passed=not owned['remaining'] and not owned['cleanup_errors'] and owned['sampler_stopped'],
                observation=owned, resource_limits_passed=owned['resource_limits_passed'])
            report['ledger_audit'].append({'path': str(path), 'outcomes': outcome})
        assert all(item['outcomes']['execution_passed'] for item in report['ledger_audit'])
        report['observation_qualified'] = all(item['outcomes']['qualified'] for item in report['ledger_audit'])
        missed = [(case['name'], case['status']) for case in report['cases'] if case['status'] != 'KILLED']
        if len(report['cases']) != len(plan) or missed:
            raise AssertionError(f'Mutation controls must reach and kill every selected mutant: {missed}')
        report['status'] = 'PASS'
    except BaseException as exc:
        report.update(status='FAIL', error={'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()})
    finally:
        signal.alarm(0); signal.signal(signal.SIGALRM, original_alarm)
        report['seconds'] = time.monotonic() - started
        if report['seconds'] > 180:
            report['status'] = 'FAIL'; report['hard_budget_exceeded'] = True
        completed = {case['name'] for case in report['cases']}
        report['not_started'] = [case['name'] for case in plan if case['name'] not in completed]
        save(output / 'summary.json', report)
    raise SystemExit(report['status'] != 'PASS')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in {'--runtime-child', '--test-child'}:
        child()
    else:
        main()
