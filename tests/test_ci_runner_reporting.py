from __future__ import annotations

import os
import json
import queue
import runpy
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CiRunnerReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cmw-ci-report-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.script = self.source / "scripts" / "ci" / "run_tests.sh"
        self.script.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts" / "ci" / "run_tests.sh", self.script)
        shutil.copyfile(ROOT / "scripts" / "ci" / "unittest_guard.py",
                        self.script.parent / "unittest_guard.py")
        (self.source / "tests").mkdir()
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
            reader.join(timeout=5)
            process.stdout.close()

        self.addCleanup(cleanup)
        reader.start()
        return process

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

    def test_progress_and_finished_module_are_live_while_another_module_waits(self) -> None:
        self.module(
            "jobs/test_ownership_fault_sweep.py",
            """
            import os
            from pathlib import Path
            import time
            import unittest

            class Waiting(unittest.TestCase):
                def test_wait(self):
                    print('..OWNERSHIP_FAULT_PROGRESS {"attempted_rows": 1}', flush=True)
                    print('Synthetic unprefixed watchdog stack frame', flush=True)
                    gate = Path(os.environ['CMW_REPORTING_GATE'])
                    deadline = time.monotonic() + 15
                    while not gate.exists():
                        if time.monotonic() > deadline:
                            raise AssertionError('fixture release was not received')
                        time.sleep(0.01)
                    print('OWNERSHIP_FAULT_SWEEP {"passed_rows": 1}', flush=True)
            """,
        )
        self.module(
            "test_fast.py",
            """
            import unittest

            class Fast(unittest.TestCase):
                def test_fast(self):
                    print('OWNERSHIP_FAULT_WARNING synthetic warning', flush=True)
                    self.assertTrue(True)
            """,
        )
        process = self.start()
        self.wait_for(
            lambda line: any("OWNERSHIP_FAULT_PROGRESS" in item for item in self.lines)
            and any(
                item.startswith("CMW_TEST_MODULE_FINISHED module=tests/test_fast.py ")
                for item in self.lines
            )
            and "Synthetic unprefixed watchdog stack frame" in self.lines
        )
        self.assertIsNone(process.poll(), "the gated module must still be running")
        self.assertFalse(any(
            line.startswith("CMW_TEST_MODULE_FINISHED module=tests/jobs/test_ownership_fault_sweep.py ")
            for line in self.lines
        ))
        self.assertIn("Synthetic unprefixed watchdog stack frame", self.lines)
        started = next(
            line for line in self.lines
            if line.startswith("CMW_TEST_MODULE_STARTED module=tests/jobs/test_ownership_fault_sweep.py ")
        )
        self.assertIn("OWNERSHIP_FAULT_PROGRESS", Path(self.fields(started)["log"]).read_text())
        fast = next(
            line for line in self.lines
            if line.startswith("CMW_TEST_MODULE_FINISHED module=tests/test_fast.py ")
        )
        fields = self.fields(fast)
        self.assertEqual((fields["result"], fields["exit_code"], fields["tests"]), ("PASS", "0", "1"))
        self.assertGreaterEqual(int(fields["elapsed_seconds"]), 0)
        self.assertIn("started_at", fields)
        self.assertIn("finished_at", fields)
        self.gate.touch()
        self.assertEqual(process.wait(timeout=10), 0)
        self.wait_for(lambda line: "2 modules, 2 tests, 0 failed modules" in line)
        self.assertEqual(sum("OWNERSHIP_FAULT_SWEEP" in line for line in self.lines), 1)
        self.assertTrue(any("OWNERSHIP_FAULT_WARNING" in line for line in self.lines))

    def test_failed_module_keeps_exit_status_and_full_failure_log(self) -> None:
        self.module(
            "test_failure.py",
            """
            import unittest

            class Failure(unittest.TestCase):
                def test_failure(self):
                    self.fail('synthetic reporting failure')
            """,
        )
        process = self.start()
        finished = self.wait_for(lambda line: line.startswith("CMW_TEST_MODULE_FINISHED "))
        fields = self.fields(finished)
        self.assertEqual((fields["result"], fields["exit_code"], fields["tests"]), ("FAIL", "1", "1"))
        self.assertEqual(process.wait(timeout=10), 1)
        self.wait_for(lambda line: "1 modules, 1 tests, 1 failed modules" in line)
        self.assertIn("synthetic reporting failure", "\n".join(self.lines))
        self.assertIn("::group::FAIL tests/test_failure.py", self.lines)

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

    def test_failure_log_is_published_before_another_module_finishes(self) -> None:
        self.module(
            'test_failure.py',
            """
            import unittest
            class Failure(unittest.TestCase):
                def test_failure(self):
                    self.fail('failure must be visible before waiting module exits')
            """,
        )
        self.module(
            'jobs/test_ownership_fault_sweep.py',
            """
            import os
            from pathlib import Path
            import time
            import unittest
            class OwnershipFaultSweepTests(unittest.TestCase):
                def test_wait(self):
                    gate = Path(os.environ['CMW_REPORTING_GATE'])
                    deadline = time.monotonic() + 15
                    while not gate.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(gate.exists())
            """,
        )
        process = self.start()
        self.wait_for(lambda line: 'AssertionError: failure must be visible' in line)
        self.assertIsNone(process.poll())
        self.assertFalse(any(line.startswith(
            'CMW_TEST_MODULE_FINISHED module=tests/jobs/test_ownership_fault_sweep.py ')
            for line in self.lines))
        self.gate.touch()
        self.assertEqual(process.wait(timeout=10), 1)

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
        records = [json.loads(line.split('CMW_TEST_CASE_STARTED ', 1)[1])
                   for line in self.lines if 'CMW_TEST_CASE_STARTED ' in line]
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
