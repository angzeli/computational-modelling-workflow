from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from cmw.adapters.hof.full_chain import _multiwfn_script


class HofFullChainTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        cmw_root = root / "fake cmw"
        workflow_directory = cmw_root / "scripts/workflows"
        workflow_directory.mkdir(parents=True)
        launch_log = root / "multiwfn launch.log"
        multiwfn = workflow_directory / "generate_igmh_cubes.sh"
        multiwfn.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" > \"$FAKE_MULTIWFN_LOG\"\n",
            encoding="utf-8",
        )
        multiwfn.chmod(0o750)

        python_wrapper = root / "python wrapper.sh"
        python_wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ ${1:-} == -m "
            "&& ${2:-} == cmw.molecular.orca.conversion_cli "
            "&& ${3:-} == plan ]]; then\n"
            "  printf '{\"result_path\":\"%s\"}\\n' \"$FAKE_CONVERSION_RESULT\"\n"
            "  exit \"${FAKE_CONVERSION_STATUS:-10}\"\n"
            "fi\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n",
            encoding="utf-8",
        )
        python_wrapper.chmod(0o750)

        density_state = root / "density state.json"
        density_state.write_text(
            json.dumps({"result_path": str(root / "density result.json")}),
            encoding="utf-8",
        )
        source_result = root / "converted wavefunction result.json"
        return cmw_root, python_wrapper, density_state, source_result

    def _run(self, root: Path, *, conversion_status: int) -> subprocess.CompletedProcess[str]:
        cmw_root, python_wrapper, density_state, source_result = self._fixture(root)
        script = root / "multiwfn step.sh"
        script.write_text(
            _multiwfn_script(
                cmw_root=cmw_root,
                python_bin=python_wrapper,
                density_state=density_state,
                output=root / "analysis output",
                converter=root / "orca_2mkl",
                multiwfn_executable=root / "Multiwfn",
                settings=root / "settings.ini",
                fragments=root / "fragments.json",
                configuration=root / "igmh.json",
                threads=8,
            ),
            encoding="utf-8",
        )
        script.chmod(0o750)
        return subprocess.run(
            (str(script),),
            cwd=root,
            env={
                **os.environ,
                "CMW_HOF_QUEUE_RUN_TOKEN": "test-queue",
                "CMW_HOF_EXPECTED_TOKEN": "test-queue",
                "FAKE_CONVERSION_RESULT": str(source_result),
                "FAKE_CONVERSION_STATUS": str(conversion_status),
                "FAKE_MULTIWFN_LOG": str(root / "multiwfn launch.log"),
            },
            capture_output=True,
            text=True,
            check=False,
        )

    def test_reusable_conversion_exit_code_reaches_multiwfn(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cmw igmh reuse ") as temporary:
            root = Path(temporary)
            completed = self._run(root, conversion_status=10)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            launch = (root / "multiwfn launch.log").read_text(encoding="utf-8")
            self.assertIn(
                f"--source {root / 'converted wavefunction result.json'}", launch
            )

    def test_nonreusable_conversion_fails_before_multiwfn(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cmw igmh missing ") as temporary:
            root = Path(temporary)
            completed = self._run(root, conversion_status=0)

            self.assertEqual(completed.returncode, 70)
            self.assertIn("result is not reusable", completed.stderr)
            self.assertFalse((root / "multiwfn launch.log").exists())

    def test_conversion_planning_error_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cmw igmh error ") as temporary:
            root = Path(temporary)
            completed = self._run(root, conversion_status=64)

            self.assertEqual(completed.returncode, 64)
            self.assertIn("planning failed before IGMH", completed.stderr)
            self.assertFalse((root / "multiwfn launch.log").exists())


if __name__ == "__main__":
    unittest.main()
