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

from cmw.core.execution_layout import ExecutionLayout
from cmw.core.locks import acquire_lock, release_lock
from cmw.core.provenance import atomic_write_json


ROOT = Path(__file__).parents[2]
RUNNER = ROOT / "scripts" / "orca" / "run_orca.sh"
FIXTURE = ROOT / "tests" / "fixtures" / "xyz" / "synthetic.xyz"


class OrcaShellTests(unittest.TestCase):
    def test_external_scratch_changes_cwd_then_copies_back_and_cleans(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cmw external shell ") as temporary:
            root = Path(temporary).resolve()
            seed = root / "seed"
            seed.mkdir()
            structure = root / "source molecule.xyz"
            shutil.copyfile(FIXTURE, structure)
            fake = root / "fake orca"
            invocation = root / "invocation.json"
            version_probe = root / "version-probe.txt"
            fake.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import pathlib
                    import sys

                    if sys.argv[1:] == ["--version"]:
                        pathlib.Path({str(version_probe)!r}).write_text("called")
                        print("synthetic ORCA 1.0")
                        raise SystemExit(0)
                    cwd = pathlib.Path.cwd()
                    stem = pathlib.Path(sys.argv[1]).stem
                    pathlib.Path({str(invocation)!r}).write_text(json.dumps({{
                        "arguments": sys.argv[1:],
                        "working_directory": str(cwd),
                        "tmpdir": os.environ.get("TMPDIR", ""),
                        "ompi_osc": os.environ.get("OMPI_MCA_osc", ""),
                        "ompi_osc_sm_backing": os.environ.get(
                            "OMPI_MCA_osc_sm_backing_directory", ""
                        ),
                    }}))
                    (cwd / f"{{stem}}.PAO_V12.tmp.proc0").write_text("disposable")
                    (cwd / f"{{stem}}.gbw").write_text("not declared persistent")
                    print("Program Version 1.0.0")
                    print("SCF CONVERGED AFTER 3 CYCLES")
                    print("FINAL SINGLE POINT ENERGY -4.000000000")
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
                "PYTHON_BIN": sys.executable,
                "ORCA_EXE": str(fake),
                "NPROCS": "1",
                "MAXCORE_MB": "64",
            }

            seed_prepare = subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "cmw.molecular.orca.cli",
                    "prepare",
                    "--stage",
                    "SP",
                    "--structure",
                    str(structure),
                    "--charge",
                    "0",
                    "--multiplicity",
                    "1",
                    "--keywords",
                    "HF STO-3G",
                    "--nprocs",
                    "1",
                    "--maxcore",
                    "64",
                    "--input",
                    str(seed / "stage.inp"),
                    "--target",
                    str(seed / "target.json"),
                ),
                cwd=ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            target_id = json.loads(seed_prepare.stdout)["target_id"]
            layout = ExecutionLayout(
                project_root=root,
                system_identifier="synthetic",
                workflow_node_identifier="sp",
                target_identifier=target_id,
                attempt_identifier="attempt_001",
            )
            layout.create_working_directory()
            atomic_write_json(layout.layout_path, layout.to_dict())
            attempt = layout.working_directory
            prepared = subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "cmw.molecular.orca.cli",
                    "prepare",
                    "--stage",
                    "SP",
                    "--structure",
                    str(structure),
                    "--charge",
                    "0",
                    "--multiplicity",
                    "1",
                    "--keywords",
                    "HF STO-3G",
                    "--nprocs",
                    "1",
                    "--maxcore",
                    "64",
                    "--input",
                    str(attempt / "stage.inp"),
                    "--target",
                    str(layout.target_directory / "target.json"),
                ),
                cwd=ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            prepared_record = json.loads(prepared.stdout)
            scratch_root = root / "external scratch"
            command = (
                str(RUNNER),
                "--input",
                str(attempt / "stage.inp"),
                "--target",
                str(layout.target_directory / "target.json"),
                "--metadata",
                str(attempt / "job.json"),
                "--output",
                str(attempt / "stage.out"),
                "--stderr",
                str(attempt / "stage.err"),
                "--layout",
                str(layout.layout_path),
                "--geometry-contract",
                str(prepared_record["geometry_contract"]),
                "--external-scratch-root",
                str(scratch_root),
                "--external-scratch-mount",
                "/",
                "--external-scratch-minimum-free-gib",
                "0.000001",
            )
            first = subprocess.run(
                command,
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(first.returncode, 0, msg=first.stderr)
            self.assertFalse(version_probe.exists())
            invocation_record = json.loads(invocation.read_text(encoding="utf-8"))
            execution_directory = Path(invocation_record["working_directory"])
            local_tmpdir = Path(invocation_record["tmpdir"])
            self.assertNotEqual(execution_directory, attempt)
            self.assertEqual(execution_directory.parent, scratch_root)
            self.assertNotEqual(local_tmpdir, execution_directory / "tmp")
            self.assertTrue(local_tmpdir.name.startswith("cmw-orca."))
            self.assertFalse(local_tmpdir.exists())
            self.assertEqual(invocation_record["ompi_osc"], "")
            self.assertEqual(
                Path(invocation_record["ompi_osc_sm_backing"]), execution_directory
            )
            self.assertFalse(execution_directory.exists())
            self.assertTrue((attempt / "stage.out").is_file())
            self.assertTrue((attempt / "stage.err").is_file())
            self.assertFalse((attempt / "stage.PAO_V12.tmp.proc0").exists())
            self.assertFalse((attempt / "stage.gbw").exists())
            scratch_record = json.loads(
                (attempt / "external-scratch.json").read_text(encoding="utf-8")
            )
            self.assertEqual(scratch_record["status"], "CLEANED")
            self.assertIn(
                f"{scratch_record['execution_stem']}.PAO_V12.tmp.proc0",
                scratch_record["copyback"]["unlisted_entry_sample"],
            )
            self.assertTrue(json.loads((attempt / "job.json").read_text())["reusable"])

            second = subprocess.run(
                command,
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(second.returncode, 0, msg=second.stderr)
            self.assertIn("[REUSED]", second.stdout)

    def test_shell_uses_local_input_name_from_space_path_and_reuses_result(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cmw shell space ") as temporary:
            directory = Path(temporary)
            structure = directory / "source molecule.xyz"
            shutil.copyfile(FIXTURE, structure)
            attempt = directory / "attempt one"
            attempt.mkdir()
            fake = directory / "fake orca"
            count = directory / "execution count.txt"
            invocation = directory / "invocation.json"
            finalize_invocation = directory / "finalize-invocation.json"
            runtime_contract = attempt / "orca-runtime.json"
            runtime_contract.write_text("{}\n", encoding="utf-8")
            runtime_bin = directory / "mpi runtime" / "bin"
            runtime_lib = directory / "mpi runtime" / "lib"
            runtime_bin.mkdir(parents=True)
            runtime_lib.mkdir(parents=True)
            python_wrapper = directory / "runtime-aware python"
            python_wrapper.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import pathlib
                    import sys

                    if sys.argv[1:4] == ["-m", "cmw.molecular.orca.cli", "runtime-validate"]:
                        print(json.dumps({{
                            "runtime_id": "synthetic-runtime",
                            "environment": {{
                                "path_prepend": [{str(runtime_bin)!r}],
                                "library_path_variable": "LD_LIBRARY_PATH",
                                "library_path_prepend": [{str(runtime_lib)!r}],
                                "variables": {{"PMIX_MCA_gds": "hash"}},
                            }},
                            "validation": {{"status": "PASSED"}},
                        }}))
                        raise SystemExit(0)
                    if sys.argv[1:4] == ["-m", "cmw.molecular.orca.cli", "runtime-materialize"]:
                        output = pathlib.Path(sys.argv[sys.argv.index("--output") + 1])
                        working = pathlib.Path(sys.argv[sys.argv.index("--working-directory") + 1])
                        record = {{
                            "runtime_id": "synthetic-runtime",
                            "working_directory": str(working),
                            "strategy": "environment_library_path",
                            "files": [],
                            "loader_probe": {{"status": "NOT_REQUIRED"}},
                            "validation": {{"status": "PASSED"}},
                        }}
                        output.write_text(json.dumps(record))
                        print(json.dumps(record))
                        raise SystemExit(0)
                    if sys.argv[1:4] == ["-m", "cmw.molecular.orca.cli", "finalize"]:
                        pathlib.Path({str(finalize_invocation)!r}).write_text(
                            json.dumps(sys.argv[4:])
                        )
                    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
                    """
                ),
                encoding="utf-8",
            )
            python_wrapper.chmod(0o755)
            fake.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import pathlib
                    import shutil
                    import sys

                    if sys.argv[1:] == ["--version"]:
                        print("synthetic ORCA 1.0")
                        raise SystemExit(0)
                    pathlib.Path({str(invocation)!r}).write_text(json.dumps({{
                        "arguments": sys.argv[1:],
                        "working_directory": str(pathlib.Path.cwd()),
                        "path": os.environ.get("PATH", ""),
                        "library_path": os.environ.get("LD_LIBRARY_PATH", ""),
                        "pmix_gds": os.environ.get("PMIX_MCA_gds", ""),
                    }}))
                    count = pathlib.Path({str(count)!r})
                    value = int(count.read_text() if count.exists() else "0") + 1
                    count.write_text(str(value))
                    print("STDIN_BYTES=" + str(len(sys.stdin.buffer.read())), file=sys.stderr)
                    print("SCRATCH=" + os.environ.get("TMPDIR", ""), file=sys.stderr)
                    source = pathlib.Path.cwd() / "input.xyz"
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
                "PYTHON_BIN": str(python_wrapper),
                "ORCA_EXE": str(fake),
                "NPROCS": "4",
                "MAXCORE_MB": "256",
                "CMW_REQUIRE_MINIMUM": "true",
                "CMW_IMAGINARY_TOLERANCE_CM1": "7.5",
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
            prepare_record = json.loads(prepare.stdout)
            self.assertIn("target_id", prepare_record)
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
                "--geometry-contract",
                str(prepare_record["geometry_contract"]),
                "--runtime-contract",
                str(runtime_contract),
                "--require-runtime-contract",
                "--artifact",
                f"final_geometry={attempt / 'stage.xyz'}",
            )
            insufficient = subprocess.run(
                (
                    *command,
                    "--minimum-free-disk-gb",
                    "999999999",
                    "--disk-check-path",
                    str(directory),
                ),
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(insufficient.returncode, 74)
            self.assertIn("FAILED_STORAGE_CAPACITY", insufficient.stderr)
            self.assertFalse(count.exists())
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
                check=False,
                capture_output=True,
                text=True,
            )
            failure_evidence = "\n".join(
                f"{path.name}:\n{path.read_text(encoding='utf-8')}"
                for path in (
                    attempt / "stage.out",
                    attempt / "stage.err",
                    attempt / "job.json",
                )
                if path.exists()
            )
            self.assertEqual(
                first.returncode,
                0,
                msg=(
                    f"stdout:\n{first.stdout}\nstderr:\n{first.stderr}\n"
                    f"{failure_evidence}"
                ),
            )
            self.assertIn("[COMPLETE]", first.stdout)
            invocation_record = json.loads(invocation.read_text(encoding="utf-8"))
            self.assertEqual(invocation_record["arguments"], ["stage.inp"])
            self.assertEqual(
                Path(invocation_record["working_directory"]), attempt.resolve()
            )
            self.assertEqual(
                invocation_record["path"].split(os.pathsep)[0], str(runtime_bin)
            )
            self.assertEqual(
                invocation_record["library_path"].split(os.pathsep)[0],
                str(runtime_lib),
            )
            self.assertEqual(invocation_record["pmix_gds"], "hash")
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
            self.assertEqual(record["geometry_input"]["mode"], "xyzfile")
            self.assertIn("runtime_contract", record["artifacts"])
            self.assertIn("runtime_launch_contract", record["artifacts"])
            self.assertTrue((attempt / "orca-runtime-launch.json").is_file())
            self.assertTrue(record["reusable"])
            finalize_arguments = json.loads(
                finalize_invocation.read_text(encoding="utf-8")
            )
            self.assertIn("--require-minimum", finalize_arguments)
            tolerance_index = finalize_arguments.index("--imaginary-tolerance")
            self.assertEqual(finalize_arguments[tolerance_index + 1], "7.5")

            second = subprocess.run(
                command, cwd=ROOT, env=env, check=True, capture_output=True, text=True
            )
            self.assertIn("[REUSED]", second.stdout)
            self.assertEqual(count.read_text(encoding="utf-8"), "1")
            self.assertFalse((attempt / "target.json.lock").exists())


if __name__ == "__main__":
    unittest.main()
