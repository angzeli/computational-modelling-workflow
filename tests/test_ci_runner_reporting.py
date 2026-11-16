from __future__ import annotations

import os
import json
import queue
import runpy
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class CiRunnerReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cmw-ci-report-"))
        self.preserve_evidence = False
        def remove_directory():
            if self.preserve_evidence:
                print(f'Fixture cleanup unresolved; evidence retained: {self.root}', flush=True)
            else:
                shutil.rmtree(self.root)
        self.addCleanup(remove_directory)
        self.source = self.root / "source"
        self.script = self.source / "scripts" / "ci" / "run_tests.sh"
        self.script.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts" / "ci" / "run_tests.sh", self.script)
        shutil.copyfile(ROOT / "scripts" / "ci" / "unittest_guard.py",
                        self.script.parent / "unittest_guard.py")
        shutil.copyfile(ROOT / "scripts" / "ci" / "run_module.py",
                        self.script.parent / "run_module.py")
        shutil.copyfile(ROOT / "scripts" / "ci" / "filter_sweep.py",
                        self.script.parent / "filter_sweep.py")
        (self.source / "tests").mkdir()
        shutil.copyfile(ROOT / 'tests' / 'fixture_processes.py',
                        self.source / 'tests' / 'fixture_processes.py')
        self.gate = self.root / "release"
        self.lines: list[str] = []
        self.output: queue.Queue[str] = queue.Queue()

    def module(self, name: str, source: str) -> None:
        path = self.source / "tests" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent != self.source / "tests":
            (path.parent / "__init__.py").write_text("", encoding="utf-8")
        path.write_text(textwrap.dedent(source), encoding="utf-8")

    def start(self, *, timeout: str = "300") -> subprocess.Popen[str]:
        process = subprocess.Popen(
            ["bash", str(self.script), str(self.root / "results")],
            env={
                **os.environ,
                "PYTHON_BIN": sys.executable,
                "PYTHONDONTWRITEBYTECODE": "1",
                "CMW_TEST_JOBS": "2",
                "CMW_TEST_TIMEOUT_SECONDS": timeout,
                "CMW_REPORTING_GATE": str(self.gate),
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        assert process.stdout is not None

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                self.lines.append(line.rstrip("\n"))
                self.output.put(line.rstrip("\n"))

        reader = threading.Thread(target=read_output, daemon=True)

        def cleanup() -> None:
            # Release the fixture even if an assertion failed. Signals are only
            # a bounded fallback for this test's own, separately grouped runner.
            try:
                self.gate.touch()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                self.cleanup_registered()
            except BaseException:
                self.preserve_evidence = True
                raise
            finally:
                reader.join(timeout=5)
                process.stdout.close()

        self.addCleanup(cleanup)
        reader.start()
        return process

    def cleanup_registered(self):
        """Keep the gate and evidence until every registered child is gone."""
        from tests.fixture_processes import read_records
        from tests.runtime.owned_process_cleanup import OwnedProcessCleanup
        owned = OwnedProcessCleanup(None, self.root)
        incomplete = []
        try:
            # Only this runner's directories. Unlike glob/exists, explicit
            # reads must surface an unreadable inventory on Python 3.14.
            try:
                modules = list((self.root / 'results').iterdir())
            except FileNotFoundError:
                modules = []
            for module in modules:
                if not module.name.startswith('m.'):
                    continue
                if not stat.S_ISDIR(module.stat().st_mode):
                    raise ValueError('Invalid fixture module directory')
                registry = module / 'evidence' / 'fixture-processes.jsonl'
                try:
                    registry.stat()
                except FileNotFoundError:
                    continue
                for record in read_records(registry.parent):
                    if record['identity'] is None:
                        incomplete.append(record)
                    else:
                        owned.remember(record['identity'])
            if not owned.wait_gone(2):
                owned.send(signal.SIGTERM, controller=True)
                self.assertTrue(owned.wait_gone(3), 'Registered fixture child did not exit; retaining evidence')
            self.assertFalse(incomplete, 'Incomplete fixture registration; retaining evidence')
        except BaseException:
            self.preserve_evidence = True
            raise

    def wait_for(self, predicate) -> str:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                line = self.output.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if predicate(line):
                return line
        self.fail("Expected live CI report was not emitted: " + "\n".join(self.lines))

    @staticmethod
    def fields(line: str) -> dict[str, str]:
        return dict(field.split("=", 1) for field in shlex.split(line)[1:])


    def test_native_timeout_names_retrying_test_and_preserves_partial_counts(self) -> None:
        self.module(
            "test_retry.py",
            """
            import time
            import unittest

            class Retry(unittest.TestCase):
                def test_a_completed(self):
                    self.assertTrue(True)

                def test_b_persistent_unavailable(self):
                    while True:
                        try:
                            raise PermissionError('stale fixture cannot observe its owner')
                        except Exception:
                            time.sleep(0.005)
            """,
        )
        self.module(
            "test_other.py",
            """
            import unittest
            class Other(unittest.TestCase):
                def test_other(self):
                    self.assertEqual(1, 1)
            """,
        )
        process = self.start(timeout="0.15")
        self.assertNotEqual(process.wait(timeout=10), 0)
        self.wait_for(lambda line: 'incomplete modules (' in line)
        output = '\n'.join(self.lines)
        self.assertIn('test_retry.Retry.test_b_persistent_unavailable', output)
        self.assertIn('Timeout (', output)
        self.assertIn('in test_b_persistent_unavailable', output)
        self.assertIn('CMW_TEST_TIMEOUT module=tests/test_retry.py', output)
        timeout = next(line for line in self.lines if line.startswith('CMW_TEST_TIMEOUT '))
        self.assertEqual(json.loads(timeout.split(' active=', 1)[1])['test'],
                         'test_retry.Retry.test_b_persistent_unavailable')
        failed = next(line for line in self.lines if line.startswith(
            'CMW_TEST_MODULE_FINISHED module=tests/test_retry.py '))
        fields = self.fields(failed)
        self.assertEqual(fields['result'], 'FAIL')
        self.assertNotEqual(fields['exit_code'], '0')
        self.assertEqual(fields['tests'], 'incomplete')
        self.assertEqual((fields['completed'], fields['started']), ('1', '2'))
        other = next(line for line in self.lines if line.startswith(
            'CMW_TEST_MODULE_FINISHED module=tests/test_other.py '))
        self.assertEqual(self.fields(other)['tests'], '1')
        self.assertIn('1 tests in completed modules', output)
        self.assertIn('1 completed methods of 2 started in incomplete modules', output)

    def test_early_zero_exit_is_incomplete_and_never_passes(self) -> None:
        self.module(
            'test_early_exit.py',
            """
            import os
            import unittest
            class EarlyExit(unittest.TestCase):
                def test_exit(self):
                    os._exit(0)
            """,
        )
        process = self.start()
        self.assertEqual(process.wait(timeout=10), 1)
        finished = self.wait_for(lambda line: line.startswith('CMW_TEST_MODULE_FINISHED '))
        fields = self.fields(finished)
        self.assertEqual((fields['result'], fields['tests']), ('FAIL', 'incomplete'))
        self.assertEqual((fields['completed'], fields['started']), ('0', '1'))

    def test_timeout_retains_registered_survivor_identity_outside_hung_interpreter(self) -> None:
        self.module(
            'test_registered_retry.py',
            """
            import os
            import subprocess
            import sys
            import time
            import unittest
            from fixture_processes import register_process

            class RegisteredRetry(unittest.TestCase):
                def test_retry(self):
                    # This child survives the parent's native timeout. Its only
                    # exit gate belongs to the outer reporting regression.
                    code = ('from pathlib import Path; import sys,time; '
                            'gate=Path(sys.argv[1]); limit=time.monotonic()+15\\n'
                            'while not gate.exists() and time.monotonic()<limit: time.sleep(.01)')
                    child = subprocess.Popen([sys.executable, '-c', code,
                        os.environ['CMW_REPORTING_GATE']], stdin=subprocess.DEVNULL)
                    assert register_process(child, role='timeout regression child') is not None
                    while True:
                        try:
                            raise PermissionError('persistent unavailable fixture')
                        except Exception:
                            time.sleep(.005)
            """,
        )
        process = self.start(timeout='1')
        self.assertNotEqual(process.wait(timeout=10), 0)
        self.wait_for(lambda line: line.startswith('CMW_TEST_FIXTURE_PROCESS '))
        record = json.loads(next(line.split(' ', 1)[1] for line in self.lines
                                 if line.startswith('CMW_TEST_FIXTURE_PROCESS ')))
        self.assertEqual(record['test'], 'test_registered_retry.RegisteredRetry.test_retry')
        self.assertEqual(record['pid'], record['identity']['pid'])
        self.assertEqual(record['identity']['version'], 2)
        self.assertTrue(record['identity']['boot_session'])
        self.assertIsInstance(record['identity']['start_token'], int)
        self.assertEqual(Path(record['registry']).stat().st_mode & 0o777, 0o600)
        from cmw.jobs.ownership import OwnershipStatus, observe_owner
        from tests.fixture_processes import audit_processes
        audit = audit_processes(Path(record['registry']).parent)
        self.assertEqual(audit[0]['status'], OwnershipStatus.VERIFIED)
        self.assertFalse(audit[0]['signal_sent'])
        self.gate.touch()
        deadline = time.monotonic() + 5
        while observe_owner(record['identity']).status != OwnershipStatus.GONE and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(observe_owner(record['identity']).status, OwnershipStatus.GONE)

    def test_registered_survivor_is_reaped_when_test_assertion_fails(self) -> None:
        class FailingAfterReceipt(CiRunnerReportingTests):
            def assertEqual(self, first, second, *args, **kwargs):
                if second == 'test_registered_retry.RegisteredRetry.test_retry':
                    self.fail('forced failure before releasing the survivor')
                return super().assertEqual(first, second, *args, **kwargs)
        case = FailingAfterReceipt(
            'test_timeout_retains_registered_survivor_identity_outside_hung_interpreter')
        result = unittest.TestResult()
        case.run(result)
        self.assertEqual(len(result.failures), 1)
        self.assertIn('forced failure before releasing the survivor', result.failures[0][1])
        self.assertEqual(result.errors, [])
        record = json.loads(next(line.split(' ', 1)[1] for line in case.lines
                                 if line.startswith('CMW_TEST_FIXTURE_PROCESS ')))
        from cmw.jobs.ownership import OwnershipStatus, observe_owner
        self.assertEqual(observe_owner(record['identity']).status, OwnershipStatus.GONE)
        self.assertFalse(case.root.exists(), 'Directory removal must follow confirmed child exit')

    def test_unreadable_cleanup_inventory_retains_evidence(self) -> None:
        for operation in ('enumeration', 'registry_stat'):
            with self.subTest(operation=operation):
                case = CiRunnerReportingTests()
                case.setUp()
                module = case.root / 'results' / 'm.fixture'
                evidence = module / 'evidence'
                evidence.mkdir(parents=True)
                registry = evidence / 'fixture-processes.jsonl'
                registry.write_text('')
                original = Path.stat
                def denied_stat(path, *args, **kwargs):
                    if path == registry:
                        raise PermissionError('unreadable registry')
                    return original(path, *args, **kwargs)
                denied = (patch.object(Path, 'iterdir', side_effect=PermissionError('unreadable results'))
                          if operation == 'enumeration' else patch.object(Path, 'stat', denied_stat))
                try:
                    with denied, self.assertRaises(PermissionError):
                        case.cleanup_registered()
                    self.assertTrue(case.preserve_evidence)
                    case.doCleanups()
                    self.assertTrue(registry.exists(), 'Unreadable cleanup must not delete its evidence')
                finally:
                    # The fault is synthetic and no child was started here.
                    shutil.rmtree(case.root)

    def test_success_cancels_native_timer_before_class_cleanup(self) -> None:
        self.module(
            'test_success.py',
            """
            import time
            import unittest
            class Success(unittest.TestCase):
                def test_one(self):
                    self.assertTrue(True)
                def test_two(self):
                    self.assertTrue(True)
                @classmethod
                def tearDownClass(cls):
                    # If stopTest leaves its timer armed, this cleanup exits.
                    time.sleep(.35)
            """,
        )
        process = self.start(timeout='0.15')
        self.assertEqual(process.wait(timeout=10), 0)
        self.wait_for(lambda line: '1 modules, 2 tests, 0 failed modules' in line)
        self.assertFalse(any('CMW_TEST_TIMEOUT' in line for line in self.lines))
        self.assertFalse(any('CMW_TEST_CASE_' in line or 'CMW_TEST_FIXTURE_PROCESS' in line
                             for line in self.lines))

    def test_only_sweep_lifecycle_class_keeps_its_existing_native_watchdog(self) -> None:
        self.module(
            'jobs/test_ownership_fault_sweep.py',
            """
            import faulthandler
            import time
            import unittest
            class OwnershipFaultSweepTests(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    faulthandler.dump_traceback_later(2, exit=True)
                def test_row(self):
                    time.sleep(.12)
                @classmethod
                def tearDownClass(cls):
                    faulthandler.cancel_dump_traceback_later()
            class ReadManifestTests(unittest.TestCase):
                def test_manifest(self):
                    self.assertTrue(True)
            """,
        )
        process = self.start(timeout='0.05')
        self.assertEqual(process.wait(timeout=10), 0)
        self.wait_for(lambda line: '1 modules, 2 tests, 0 failed modules' in line)
        private_log = next((self.root / 'results').glob('m.*/log')).read_text().splitlines()
        records = [json.loads(line.split('CMW_TEST_CASE_STARTED ', 1)[1])
                   for line in private_log if 'CMW_TEST_CASE_STARTED ' in line]
        policies = {record['test']: record['watchdog'] for record in records}
        self.assertEqual(policies['jobs.test_ownership_fault_sweep.OwnershipFaultSweepTests.test_row'],
                         'sweep-per-row')
        self.assertEqual(policies['jobs.test_ownership_fault_sweep.ReadManifestTests.test_manifest'],
                         'native-per-test')

    def test_timeout_configuration_requires_positive_finite_seconds(self) -> None:
        configuration = runpy.run_path(str(ROOT / 'scripts' / 'ci' / 'unittest_guard.py'))
        parse = configuration['timeout_seconds']
        self.assertEqual(configuration['DEFAULT_TIMEOUT_SECONDS'], 300.0)
        self.assertEqual(parse('0.25'), 0.25)
        for value in ('', 'not-a-number', '0', '-1', 'nan', 'inf', '-inf'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'positive finite'):
                parse(value)


if __name__ == "__main__":
    unittest.main()
