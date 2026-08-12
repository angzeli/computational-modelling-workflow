from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from cmw.core.locks import acquire_lock, release_lock


ROOT = Path(__file__).parents[2]
RUNNER = ROOT / "scripts" / "orca" / "run_orca.sh"
FIXTURE = ROOT / "tests" / "fixtures" / "xyz" / "synthetic.xyz"


class OrcaShellTests(unittest.TestCase):
    def test_shell_runs_fake_orca_with_isolated_stdin_and_reuses_result(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cmw shell space ") as temporary:
            directory = Path(temporary)
            structure = directory / "source molecule.xyz"
            shutil.copyfile(FIXTURE, structure)
            attempt = directory / "attempt one"
            attempt.mkdir()
            fake = directory / "fake orca"
            count = directory / "execution count.txt"
            fake.write_text(
                textwrap.dedent(
                    f"""\
                    #!{sys.executable}
                    import os
                    import pathlib
                    import shutil
                    import sys

                    if sys.argv[1:] == ["--version"]:
                        print("synthetic ORCA 1.0")
                        raise SystemExit(0)
                    count = pathlib.Path({str(count)!r})
                    value = int(count.read_text() if count.exists() else "0") + 1
                    count.write_text(str(value))
                    print("STDIN_BYTES=" + str(len(sys.stdin.buffer.read())), file=sys.stderr)
                    print("SCRATCH=" + os.environ.get("TMPDIR", ""), file=sys.stderr)
                    source = pathlib.Path(sys.argv[1]).parent / "input.xyz"
                    shutil.copyfile(source, pathlib.Path(sys.argv[1]).with_suffix(".xyz"))
                    print("Program Version 1.0.0")
                    print("SCF CONVERGED AFTER 3 CYCLES")
                    print("FINAL SINGLE POINT ENERGY -4.000000000")
                    print("THE OPTIMIZATION HAS CONVERGED")
                    print("ORCA TERMINATED NORMALLY")
                    """
                ),
                encoding="utf-8",
            )
            fake.chmod(0o755)
            env = {
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(ROOT / "src"),
                "ORCA_EXE": str(fake),
                "NPROCS": "4",
                "MAXCORE_MB": "256",
            }
            prepare = subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "cmw.molecular.orca.cli",
                    "prepare",
                    "--stage",
                    "OPT",
                    "--structure",
                    str(structure),
                    "--charge",
                    "0",
                    "--multiplicity",
                    "1",
                    "--keywords",
                    "HF STO-3G",
                    "--nprocs",
                    "4",
                    "--maxcore",
                    "256",
                    "--input",
                    str(attempt / "stage.inp"),
                    "--target",
                    str(attempt / "target.json"),
                ),
                cwd=ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("target_id", json.loads(prepare.stdout))
            command = (
                str(RUNNER),
                "--input",
                str(attempt / "stage.inp"),
                "--target",
                str(attempt / "target.json"),
                "--metadata",
                str(attempt / "job.json"),
                "--output",
                str(attempt / "stage.out"),
                "--stderr",
                str(attempt / "stage.err"),
                "--artifact",
                f"final_geometry={attempt / 'stage.xyz'}",
            )
            held_lock = acquire_lock(
                attempt / "target.json.lock", job_id="held", owner_pid=os.getpid()
            )
            collision = subprocess.run(
                command, cwd=ROOT, env=env, check=False, capture_output=True, text=True
            )
            self.assertEqual(collision.returncode, 73)
            self.assertFalse(count.exists())
            self.assertTrue(
                release_lock(attempt / "target.json.lock", token=held_lock.token)
            )
            first = subprocess.run(
                command,
                cwd=ROOT,
                env=env,
                input="manifest data that ORCA must not consume\n",
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("[COMPLETE]", first.stdout)
            error_text = (attempt / "stage.err").read_text(encoding="utf-8")
            self.assertIn("STDIN_BYTES=0", error_text)
            scratch = next(
                line.removeprefix("SCRATCH=")
                for line in error_text.splitlines()
                if line.startswith("SCRATCH=")
            )
            self.assertFalse(Path(scratch).exists())
            record = json.loads((attempt / "job.json").read_text(encoding="utf-8"))
            self.assertEqual(record["attempt"]["resources"]["nprocs"], 4)
            self.assertTrue(record["reusable"])

            second = subprocess.run(
                command, cwd=ROOT, env=env, check=True, capture_output=True, text=True
            )
            self.assertIn("[REUSED]", second.stdout)
            self.assertEqual(count.read_text(encoding="utf-8"), "1")
            self.assertFalse((attempt / "target.json.lock").exists())


if __name__ == "__main__":
    unittest.main()
