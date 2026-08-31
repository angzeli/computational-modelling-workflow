from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cmw.core.execution_profiles import execution_profiles_from_mapping
from cmw.core.process_health import (
    HealthAssessment,
    HealthReport,
    HealthState,
    ProcessTreeSnapshot,
)
from cmw.molecular.multiwfn.automation import Operation, menu_stream
from cmw.molecular.multiwfn.runtime import (
    DEFAULT_MULTIWFN_NTHREADS,
    MENU_CONTRACT,
    inspect_runtime_compatibility,
    parallelism_warning,
    parse_threads,
    prepare_runtime,
    resolve_executable,
    resolve_menu_contract,
    resolve_threads,
)


ROOT = Path(__file__).resolve().parents[3]
SHELL = ROOT / "scripts" / "multiwfn" / "multiwfn_runtime.sh"


def _execution_profile():
    return execution_profiles_from_mapping(
        {
            "schema_version": 1,
            "active_profile": "local_mac",
            "profiles": {
                "local_mac": {
                    "orca": {"nprocs": 8, "total_memory_gb": 18},
                    "multiwfn": {"nthreads": 8, "total_memory_gb": 18},
                }
            },
        }
    ).selected


def _fake(root: Path, name: str = "Multiwfn", version: str = "3.8") -> Path:
    path = root / name
    path.write_text(
        f"""#!/usr/bin/env python3
import os, sys
if '--version' in sys.argv:
    print('Multiwfn -- synthetic\\nVersion {version}')
    raise SystemExit(0)
data = sys.stdin.read()
print('STDIN=' + data.replace('\\n', ','))
print('THREADS=' + os.environ.get('OMP_NUM_THREADS', ''))
print('PATH=' + os.environ.get('Multiwfnpath', ''))
if len(sys.argv) > 1:
    source = sys.argv[1]
    print('SOURCE=' + source)
    print('SOURCE_TEXT=' + open(source, encoding='utf-8').read().strip())
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


class RuntimeTests(unittest.TestCase):
    def test_thread_default_override_and_conflict(self) -> None:
        self.assertEqual(DEFAULT_MULTIWFN_NTHREADS, 8)
        self.assertEqual(resolve_threads(environment={}), 8)
        self.assertEqual(resolve_threads(environment={"MULTIWFN_NTHREADS": "4"}), 4)
        self.assertEqual(resolve_threads(cli_value=4, environment={}, config_value=4), 4)
        with self.assertRaisesRegex(ValueError, "contradictory"):
            resolve_threads(cli_value=4, environment={"MULTIWFN_NTHREADS": "8"})
        for value in ("", "0", "-1", "four"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_threads(value)

    def test_executable_resolution_missing_and_path_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cmw exe space ") as temporary:
            executable = _fake(Path(temporary), "Multiwfn test")
            self.assertEqual(resolve_executable(explicit=str(executable)), executable.resolve())
            with self.assertRaises(FileNotFoundError):
                resolve_executable(explicit=str(Path(temporary) / "missing"))

    def test_settings_are_immutable_and_attempts_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = _fake(root)
            source = root / "settings.ini"
            original = b"alpha=1\nnthreads= 2 // keep\nomega=3\n"
            source.write_bytes(original)
            first = root / "attempt one"
            second = root / "attempt two"
            first.mkdir()
            second.mkdir()
            one = prepare_runtime(
                attempt_directory=first,
                executable=str(executable),
                settings_source=source,
                cli_threads=1,
                environment={},
            )
            four = prepare_runtime(
                attempt_directory=second,
                executable=str(executable),
                settings_source=source,
                cli_threads=4,
                environment={},
            )
            self.assertEqual(source.read_bytes(), original)
            self.assertIn("nthreads= 1", Path(one.settings_path).read_text())
            self.assertIn("nthreads= 4", Path(four.settings_path).read_text())
            self.assertNotEqual(Path(one.settings_path), Path(four.settings_path))
            self.assertEqual(Path(one.settings_path).stat().st_mode & 0o222, 0)
            self.assertEqual(one.version, "3.8")
            self.assertEqual(one.menu_contract, MENU_CONTRACT)
            self.assertEqual(one.executable_sha256, four.executable_sha256)

    def test_execution_profile_threads_use_existing_runtime_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = _fake(root)
            settings = root / "settings.ini"
            settings.write_text("nthreads= 2\n", encoding="utf-8")
            attempt = root / "attempt"
            attempt.mkdir()
            profile = _execution_profile()

            runtime = prepare_runtime(
                attempt_directory=attempt,
                executable=str(executable),
                settings_source=settings,
                execution_profile=profile,
                environment={},
            )
            record = runtime.to_dict()

            self.assertEqual(runtime.requested_nthreads, 8)
            self.assertIn("nthreads= 8", Path(runtime.settings_path).read_text())
            self.assertEqual(record["execution_profile"], "local_mac")
            self.assertEqual(record["execution_profile_hash"], profile.execution_profile_hash)
            self.assertEqual(record["total_memory_gb"], 18.0)
            self.assertNotIn("maxcore", record)

            conflicting = root / "conflicting"
            conflicting.mkdir()
            with self.assertRaisesRegex(ValueError, "contradictory"):
                prepare_runtime(
                    attempt_directory=conflicting,
                    executable=str(executable),
                    settings_source=settings,
                    cli_threads=4,
                    execution_profile=profile,
                    environment={},
                )

    def test_unsupported_version_fails_before_launch(self) -> None:
        with self.assertRaisesRegex(ValueError, "verified versions are 3.8"):
            menu_stream(Operation.ESP, "3.9", {"grid_spacing_bohr": 0.2})

    def test_verified_calendar_release_uses_existing_menu_contract(self) -> None:
        legacy = menu_stream(
            Operation.IGMH,
            "3.8",
            {"fragment_a": [1, 2], "fragment_b": [3, 4], "grid_spacing_bohr": 0.2},
        )
        calendar = menu_stream(
            Operation.IGMH,
            "2026.7.15",
            {"fragment_a": [1, 2], "fragment_b": [3, 4], "grid_spacing_bohr": 0.2},
        )

        self.assertEqual(resolve_menu_contract("2026.7.15"), MENU_CONTRACT)
        self.assertEqual(calendar, legacy)
        with self.assertRaisesRegex(ValueError, "unsupported Multiwfn menu contract"):
            resolve_menu_contract("2026.7.16")

    def test_calendar_release_is_validated_during_runtime_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = _fake(root, version="2026.7.15")
            settings = root / "settings.ini"
            settings.write_text("nthreads= 2\n", encoding="utf-8")

            record = inspect_runtime_compatibility(
                executable=str(executable),
                settings_source=settings,
                threads=8,
                environment={},
            )

            self.assertEqual(record["version"], "2026.7.15")
            self.assertEqual(record["menu_contract"], MENU_CONTRACT)
            self.assertEqual(record["requested_nthreads"], 8)
            self.assertEqual(record["validation"]["status"], "PASSED")

    def test_operation_stream_forwards_semantic_values(self) -> None:
        text = menu_stream(
            Operation.IGMH,
            "3.8",
            {"fragment_a": [1, 2], "fragment_b": [3, 4], "grid_spacing_bohr": 0.2},
        )
        self.assertIn("1,2", text)
        self.assertIn("3,4", text)
        self.assertIn("0.2", text)

    def test_documented_fmo_and_esp_profiles_use_batch_exports(self) -> None:
        fmo = menu_stream(
            Operation.FMO,
            "3.8",
            {"homo_index": 5, "lumo_index": 6, "grid_spacing_bohr": 0.2},
        ).splitlines()
        self.assertEqual(fmo, ["200", "3", "5,6", "4", "0.2", "1", "0", "q"])
        esp = menu_stream(
            Operation.ESP, "3.8", {"grid_spacing_bohr": 0.2}
        ).splitlines()
        self.assertEqual(
            esp,
            ["5", "1", "4", "0.2", "2", "0", "5", "12", "4", "0.2", "2", "0", "q"],
        )

    def test_parallelism_warning_is_diagnostic_only(self) -> None:
        snapshot = ProcessTreeSnapshot(7, 10.0, True, True, ())
        assessment = HealthAssessment(
            HealthState.ACTIVE, "cpu", 0, 10.0, 10.0, False, 0
        )
        observed = parallelism_warning(8, HealthReport(snapshot, (), assessment))
        self.assertIn("diagnostic only", observed.warning or "")
        unavailable = HealthAssessment(
            HealthState.UNKNOWN, "unavailable", 0, None, None, False, 0
        )
        self.assertIsNone(
            parallelism_warning(8, HealthReport(snapshot, (), unavailable)).warning
        )

    def test_shell_short_alias_stdin_threads_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cmw shell space ") as temporary:
            root = Path(temporary)
            executable = _fake(root, "fake Multiwfn")
            settings = root / "settings.ini"
            settings.write_text("nthreads= 2\n", encoding="utf-8")
            attempt = root / "attempt directory"
            attempt.mkdir()
            metadata = attempt / "runtime.json"
            completed = subprocess.run(
                (
                    "bash",
                    "-c",
                    'set -euo pipefail; source "$1"; MULTIWFN_EXE="$2"; '
                    'MULTIWFN_NTHREADS=4; multiwfn_runtime_prepare "$3" "$4" "$5"; '
                    'cd "$3"; printf "hello\\n" | multiwfn_runtime_launch; '
                    'printf "again\\n" | multiwfn_runtime_launch',
                    "_",
                    str(SHELL),
                    str(executable),
                    str(attempt),
                    str(metadata),
                    str(settings),
                ),
                cwd=ROOT,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("STDIN=hello,", completed.stdout)
            self.assertIn("THREADS=4", completed.stdout)
            aliases = [
                line.removeprefix("PATH=")
                for line in completed.stdout.splitlines()
                if line.startswith("PATH=")
            ]
            self.assertEqual(len(aliases), 2)
            self.assertEqual(len(set(aliases)), 2)
            self.assertTrue(all(not Path(alias).exists() for alias in aliases))
            record = json.loads(metadata.read_text())
            self.assertEqual(record["runtime"]["requested_nthreads"], 4)

    def test_shell_uses_short_source_alias_for_long_project_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cmw shell source space ") as temporary:
            root = Path(temporary)
            executable = _fake(root, "fake Multiwfn")
            settings = root / "settings.ini"
            settings.write_text("nthreads= 2\n", encoding="utf-8")
            attempt = root / "attempt directory"
            attempt.mkdir()
            metadata = attempt / "runtime.json"
            source_directory = root / ("long source directory " + "x" * 180)
            source_directory.mkdir()
            source = source_directory / "wavefunction.molden.input"
            source.write_text("[Molden Format]\n", encoding="utf-8")

            completed = subprocess.run(
                (
                    "bash",
                    "-c",
                    'set -euo pipefail; source "$1"; MULTIWFN_EXE="$2"; '
                    'MULTIWFN_NTHREADS=4; multiwfn_runtime_prepare "$3" "$4" "$5"; '
                    'cd "$3"; printf "hello\\n" | multiwfn_runtime_launch "$6"',
                    "_",
                    str(SHELL),
                    str(executable),
                    str(attempt),
                    str(metadata),
                    str(settings),
                    str(source),
                ),
                cwd=ROOT,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True,
                text=True,
                check=True,
            )

            source_argument = next(
                line.removeprefix("SOURCE=")
                for line in completed.stdout.splitlines()
                if line.startswith("SOURCE=")
            )
            self.assertLess(len(source_argument), 160)
            self.assertNotIn(" ", source_argument)
            self.assertIn("SOURCE_TEXT=[Molden Format]", completed.stdout)
            self.assertFalse(Path(source_argument).exists())
            alias_record = (attempt / "multiwfn-runtime-alias.txt").read_text()
            self.assertIn("settings=", alias_record)
            self.assertIn("settings_target=", alias_record)
            settings_target = next(
                line.removeprefix("settings_target=")
                for line in alias_record.splitlines()
                if line.startswith("settings_target=")
            )
            self.assertTrue(Path(settings_target, "settings.ini").is_file())
            self.assertIn("source=", alias_record)
            self.assertIn(f"source_target={source.resolve()}", alias_record)

    def test_shell_forwards_termination_and_cleans_aliases(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cmw shell signal ") as temporary:
            root = Path(temporary)
            executable = root / "fake Multiwfn"
            executable.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ ${1:-} == --version ]]; then "
                "printf 'Multiwfn -- synthetic\\nVersion 3.8\\n'; exit 0; fi\n"
                "printf '%s\\n' $$ > \"$CMW_FAKE_PID_FILE\"\n"
                "exec sleep 30\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            settings = root / "settings.ini"
            settings.write_text("nthreads= 2\n", encoding="utf-8")
            attempt = root / "attempt"
            attempt.mkdir()
            pid_file = root / "child.pid"
            process = subprocess.Popen(
                (
                    "bash",
                    "-c",
                    'set -euo pipefail; source "$1"; MULTIWFN_EXE="$2"; '
                    'MULTIWFN_NTHREADS=4; MULTIWFN_RUN_SETTINGS_PATH="$4"; '
                    'MULTIWFN_RUN_SETTINGS_DIRECTORY=${MULTIWFN_RUN_SETTINGS_PATH%/settings.ini}; '
                    'cd "$3"; multiwfn_runtime_launch',
                    "_",
                    str(SHELL),
                    str(executable),
                    str(attempt),
                    str(settings),
                ),
                cwd=ROOT,
                env={
                    **os.environ,
                    "CMW_FAKE_PID_FILE": str(pid_file),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(100):
                if pid_file.is_file():
                    break
                time.sleep(0.02)
            self.assertTrue(pid_file.is_file())
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            process.terminate()
            process.wait(timeout=5)
            process.communicate(timeout=1)
            child_gone = False
            for _ in range(100):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    child_gone = True
                    break
                time.sleep(0.02)
            self.assertTrue(child_gone)
            aliases = dict(
                line.split("=", 1)
                for line in (attempt / "multiwfn-runtime-alias.txt")
                .read_text(encoding="utf-8")
                .splitlines()
            )
            self.assertFalse(Path(aliases["settings"]).exists())


if __name__ == "__main__":
    unittest.main()
