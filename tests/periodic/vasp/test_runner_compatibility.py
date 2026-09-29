"""Static handoff checks must not execute the runner they inspect."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cmw.cli import main
from cmw.periodic.vasp.runner_compatibility import check_runner


class RunnerCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cmw-runner-check-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.port = self.root / "port"
        self.scripts = self.port / "scripts"
        self.scripts.mkdir(parents=True)
        self.runner = self.scripts / "run-vasp.sh"
        self.runner.write_text(
            '#!/usr/bin/env bash\nset -euo pipefail\n'
            '. "$(dirname -- "$0")/environment.sh"\n'
            'exec python3 -B "$PORT_ROOT/scripts/run_vasp.py" "$@"\n'
        )
        self.runner.chmod(0o700)
        (self.scripts / "environment.sh").write_text(
            'PORT_ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)\n'
        )
        for name in ("launch.py", "observables.py"):
            (self.scripts / name).write_text('"""Harmless synthetic source fixture."""\n')
        self.entrypoint = self.scripts / "run_vasp.py"
        self.write_parser()
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        for name in ("INCAR", "POSCAR", "KPOINTS", "POTCAR"):
            (self.inputs / name).write_text("Invented non-scientific input fixture\n")
        self.output = self.root / "new-output"
        self.argv = ["--input", str(self.inputs),
                     "--output", str(self.output), "--binary", "std", "--ranks", "2",
                     "--ncore", "1", "--kpar", "1", "--mpi-mode", "native",
                     "--restart", "none", "--timeout", "10", "--stop-before", "0",
                     "--managed-foreground"]

    def write_parser(self, *, managed=True, suffix="", prefix=""):
        declarations = [
            'p.add_argument("--input", required=True)',
            'p.add_argument("--output", required=True)',
            'p.add_argument("--binary", choices=("std",), default="std")',
            'p.add_argument("--ranks", type=int, required=True)',
            'p.add_argument("--ncore", type=int, required=True)',
            'p.add_argument("--kpar", type=int, required=True)',
            'p.add_argument("--mpi-mode", choices=("native", "synthetic"))',
            'p.add_argument("--restart", choices=("none",))',
            'p.add_argument("--timeout", type=float)',
            'p.add_argument("--stop-before", type=float)',
        ]
        if managed:
            declarations.append('p.add_argument("--managed-foreground", action="store_true")')
        body = "\n".join("    " + line for line in declarations)
        self.entrypoint.write_text(
            prefix + 'import argparse\n\ndef parser():\n'
            '    p = argparse.ArgumentParser(description="Invented fixture")\n'
            + body + "\n" + suffix + "    return p\n"
        )

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*")
                if path.is_file() and ".git" not in path.relative_to(self.root).parts}

    def check(self, argv=None, *, runner=None, project_root=None):
        original_popen = subprocess.Popen

        def source_identity_only(command, *args, **kwargs):
            self.assertIsInstance(command, (list, tuple))
            self.assertEqual(Path(command[0]).name, "git", "Runner probe must not execute anything")
            self.assertFalse(kwargs.get("shell", False))
            return original_popen(command, *args, **kwargs)

        before = self.snapshot()
        with patch("subprocess.Popen", side_effect=source_identity_only), \
                patch("cmw.jobs.store.Store", side_effect=AssertionError("No Jobs mutation")):
            result = check_runner(runner or self.runner, self.argv if argv is None else argv,
                                  project_root=project_root)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(result["side_effects_performed"], [])
        self.assertEqual(result["managed_lifecycle_qualification"]["status"], "NOT_ESTABLISHED")
        self.assertEqual(result["result_binding_qualification"]["status"], "NOT_ESTABLISHED")
        return result

    def test_compatible_declaration_does_not_qualify_runtime_or_binding(self):
        result = self.check()
        self.assertEqual(result["interface_compatibility"]["status"], "COMPATIBLE", result)
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["observed_capabilities"])
        self.assertTrue(result["source_combination"])
        self.assertTrue(result["unassessed_properties"])
        self.assertFalse(self.output.exists())

    def test_absent_managed_argument_has_actionable_incompatibility(self):
        self.write_parser(managed=False)
        result = self.check()
        self.assertEqual(result["interface_compatibility"]["status"], "INCOMPATIBLE")
        self.assertEqual(result["exit_code"], 2)
        self.assertIn("managed-foreground", json.dumps(result["missing_requirements"]))

    def test_help_description_mention_is_not_argument_support(self):
        self.write_parser(managed=False)
        self.entrypoint.write_text(self.entrypoint.read_text().replace(
            'description="Invented fixture"',
            'description="Documentation mentions --managed-foreground; not implemented"'))
        result = self.check()
        self.assertEqual(result["interface_compatibility"]["status"], "INCOMPATIBLE")
        self.assertIn("managed-foreground", json.dumps(result["missing_requirements"]))

    def test_arbitrary_module_body_is_never_imported_or_executed(self):
        marker = self.root / "forbidden-import-marker"
        self.write_parser(prefix=f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        self.check()
        self.assertFalse(marker.exists())

    def test_dynamic_parser_construction_is_unknown(self):
        self.write_parser(suffix="    configure_parser(p)\n")
        result = self.check()
        self.assertEqual(result["interface_compatibility"]["status"], "UNKNOWN")
        self.assertEqual(result["exit_code"], 2)

    def test_omitted_extra_required_option_is_incompatible(self):
        self.write_parser(suffix='    p.add_argument("--required-token", required=True)\n')
        result = self.check()
        self.assertEqual(result["interface_compatibility"]["status"], "INCOMPATIBLE")
        self.assertEqual(result["exit_code"], 2)
        self.assertIn("required-token", json.dumps(result["missing_requirements"]))

    def test_dynamic_required_or_type_constraint_remains_unknown(self):
        original = self.entrypoint.read_text()
        replacements = (
            ('"--input", required=True', '"--input", required=required_at_runtime()'),
            ('"--ranks", type=int', '"--ranks", type=custom_rank_converter'),
        )
        for old, new in replacements:
            with self.subTest(declaration=new):
                self.entrypoint.write_text(original.replace(old, new))
                result = self.check()
                self.assertEqual(result["interface_compatibility"]["status"], "UNKNOWN")
                self.assertEqual(result["exit_code"], 2)

    def test_literal_but_incompatible_argument_declarations_are_rejected(self):
        original = self.entrypoint.read_text()
        replacements = (
            ('choices=("native", "synthetic")', 'choices=("synthetic",)'),
            ('action="store_true"', 'action="store_false"'),
            ('"--input", required=True', '"--input", required=True, nargs="+"'),
        )
        for old, new in replacements:
            with self.subTest(declaration=new):
                self.entrypoint.write_text(original.replace(old, new))
                result = self.check()
                self.assertEqual(result["interface_compatibility"]["status"], "INCOMPATIBLE")
                self.assertEqual(result["exit_code"], 2)
                self.assertTrue(result["missing_requirements"])

    def test_unknown_wrapper_is_not_probed(self):
        marker = self.root / "forbidden-wrapper-marker"
        self.runner.write_text(f"#!/bin/sh\ntouch '{marker}'\nexec /bin/false\n")
        result = self.check()
        self.assertEqual(result["interface_compatibility"]["status"], "UNKNOWN")
        self.assertEqual(result["exit_code"], 2)
        self.assertFalse(marker.exists())

    def test_redirected_port_root_is_unknown(self):
        redirected = self.root / "other-port"
        (self.scripts / "environment.sh").write_text(f"PORT_ROOT={shlex.quote(str(redirected))}\n")
        result = self.check()
        self.assertEqual(result["interface_compatibility"]["status"], "UNKNOWN")
        self.assertEqual(result["exit_code"], 2)
        self.assertFalse(redirected.exists())

    def test_extra_environment_command_is_unknown_and_never_runs(self):
        marker = self.root / "forbidden-environment-marker"
        environment = self.scripts / "environment.sh"
        environment.write_text(environment.read_text() + f"touch {shlex.quote(str(marker))}\n")
        result = self.check()
        self.assertEqual(result["interface_compatibility"]["status"], "UNKNOWN")
        self.assertEqual(result["exit_code"], 2)
        self.assertFalse(marker.exists())

    def test_known_inert_compiler_assignment_preserves_wrapper_association(self):
        environment = self.scripts / "environment.sh"
        environment.write_text(environment.read_text()
                               + 'PORT_GNU=/invented/compiler\n'
                               + 'export OMPI_CXX="$PORT_GNU/bin/g++-16"\n')
        result = self.check()
        self.assertEqual(result["interface_compatibility"]["status"], "COMPATIBLE", result)
        self.assertEqual(result["exit_code"], 0)

    def test_wrong_wrapper_duplicate_unsupported_and_invalid_route_arguments(self):
        wrong = self.scripts / "different-wrapper.sh"
        wrong.write_bytes(self.runner.read_bytes())
        result = self.check(runner=wrong)
        self.assertEqual(result["interface_compatibility"]["status"], "INCOMPATIBLE")
        self.assertEqual(result["exit_code"], 2)
        variants = []
        variants.append(("duplicate option", self.argv + ["--output", str(self.output)]))
        variants.append(("unsupported threads", self.argv + ["--threads", "2"]))
        missing = self.argv[:-1]
        variants.append(("managed option omitted", missing))
        for label, key, value in (("relative input", "--input", "relative-input"),
                                  ("missing input", "--input", str(self.root / "missing-input")),
                                  ("relative output", "--output", "relative-output"),
                                  ("bad ranks", "--ranks", "64"),
                                  ("nonfinite timeout", "--timeout", "NaN")):
            argv = list(self.argv)
            argv[argv.index(key) + 1] = value
            variants.append((label, argv))
        for label, argv in variants:
            with self.subTest(label=label):
                result = self.check(argv)
                self.assertEqual(result["interface_compatibility"]["status"], "INCOMPATIBLE", result)
                self.assertEqual(result["exit_code"], 2)
                self.assertTrue(result["missing_requirements"])
        self.assertFalse(self.output.exists())

    def test_existing_output_is_refused_without_changes(self):
        self.output.mkdir()
        (self.output / "retained.txt").write_text("Preserve existing output\n")
        result = self.check()
        self.assertEqual(result["interface_compatibility"]["status"], "INCOMPATIBLE")
        self.assertEqual(result["exit_code"], 2)

    def test_cli_json_and_human_keep_the_three_dimensions_separate(self):
        arguments = ["vasp", "check-runner", "--runner", str(self.runner)]
        output = io.StringIO()
        before = self.snapshot()
        with redirect_stdout(output):
            code = main([*arguments, "--json", "--", *self.argv])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0, result)
        self.assertEqual(result["interface_compatibility"]["status"], "COMPATIBLE")
        self.assertEqual(result["managed_lifecycle_qualification"]["status"], "NOT_ESTABLISHED")
        self.assertEqual(result["result_binding_qualification"]["status"], "NOT_ESTABLISHED")
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([*arguments, "--", *self.argv])
        self.assertEqual(code, 0)
        human = output.getvalue().lower()
        self.assertIn("interface", human)
        self.assertIn("lifecycle", human)
        self.assertIn("binding", human)
        self.assertIn("not", human)
        self.assertEqual(before, self.snapshot())

    def git(self, *arguments):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                   "-c", "init.templateDir=", "-c", "core.attributesFile=/dev/null", "-C", str(self.port)]
        return subprocess.run([*command, *arguments], check=True, env=environment,
                              capture_output=True, text=True).stdout.strip()

    def initialize_git(self):
        for arguments in (("init", "-q"), ("add", "scripts"),
                          ("-c", "user.name=CMW Synthetic Fixture", "-c",
                           "user.email=synthetic@example.invalid", "commit", "-qm", "Synthetic runner fixture")):
            self.git(*arguments)
        return self.git("rev-parse", "HEAD")

    def test_source_identity_distinguishes_committed_and_dirty_runner(self):
        head = self.initialize_git()
        clean = self.check(project_root=self.port)
        clean_source = clean["source_combination"]["runner"]
        self.assertEqual(clean_source["commit"], head)
        self.assertIsNone(clean_source["tracked_worktree_dirty"])
        self.assertFalse(clean_source["relevant_source_dirty"])
        self.assertEqual(clean_source["untracked_relevant_sources"], [])
        self.assertTrue(clean_source["sources"])
        self.assertEqual(clean["source_combination"]["project"]["commit"], head)
        self.entrypoint.write_text(self.entrypoint.read_text() + "\n# Local uncommitted edit\n")
        dirty = self.check()
        dirty_source = dirty["source_combination"]["runner"]
        self.assertEqual(dirty_source["commit"], head)
        self.assertIsNone(dirty_source["tracked_worktree_dirty"])
        self.assertTrue(dirty_source["relevant_source_dirty"])
        self.assertEqual(dirty_source["untracked_relevant_sources"], [])
        self.assertIn("committed", dirty["interface_compatibility"]["source_warning"])

    def test_source_identity_does_not_execute_configured_clean_filter(self):
        self.initialize_git()
        marker = self.root / "forbidden-git-filter-marker"
        filter_script = self.root / "configured-filter.py"
        filter_script.write_text(
            f"import sys\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n"
            "sys.stdout.write(sys.stdin.read())\n"
        )
        (self.port / ".gitattributes").write_text("scripts/*.py filter=unsafe-fixture\n")
        self.git("config", "filter.unsafe-fixture.clean", shlex.join([sys.executable, str(filter_script)]))
        self.entrypoint.write_text(self.entrypoint.read_text() + "\n# Different source bytes\n")
        result = self.check()
        self.assertTrue(result["source_combination"]["runner"]["relevant_source_dirty"])
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
