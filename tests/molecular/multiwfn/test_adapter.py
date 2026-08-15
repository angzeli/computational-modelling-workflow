from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cmw.core.provenance import file_hash
from cmw.molecular.multiwfn.adapter import (
    MultiwfnAdapterError,
    MultiwfnCommandSpec,
    MultiwfnOutputSpec,
    build_command_spec,
    discover_outputs,
    validate_command_spec,
)


class MultiwfnAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cmw multiwfn adapter ")
        self.root = Path(self.temporary.name)
        self.executable = self.root / "Multiwfn synthetic"
        self.executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.executable.chmod(0o755)
        self.settings = self.root / "settings.ini"
        self.settings.write_text("nthreads= 2\n", encoding="utf-8")
        self.source = self.root / "source.molden.input"
        self.source.write_text("[Molden Format]\n", encoding="utf-8")
        self.menu = self.root / "menu.in"
        self.menu.write_text("20\n11\nq\n", encoding="utf-8")
        self.runtime = {
            "executable": str(self.executable),
            "executable_sha256": file_hash(self.executable),
            "version": "3.8",
            "requested_nthreads": 4,
            "settings_mechanism": "run_local_multiwfnpath",
            "settings_path": str(self.settings),
            "settings_sha256": file_hash(self.settings),
        }
        self.outputs = (
            MultiwfnOutputSpec(
                "delta_g_inter_cube",
                "raw/inter.cub",
                "visualization/inter.cube",
            ),
            MultiwfnOutputSpec(
                "sign_lambda2_rho_cube",
                "raw/sl2r.cub",
                "visualization/sl2r.cube",
            ),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_command_generation_is_deterministic_and_provenance_complete(self) -> None:
        first = build_command_spec(
            runtime=self.runtime,
            source_path=self.source,
            attempt_directory=self.root,
            stdin_path=self.menu,
            outputs=self.outputs,
        )
        second = build_command_spec(
            runtime=self.runtime,
            source_path=self.source,
            attempt_directory=self.root,
            stdin_path=self.menu,
            outputs=self.outputs,
        )

        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(
            first.argv,
            (str(self.executable.resolve()), str(self.source.resolve())),
        )
        self.assertEqual(first.environment["OMP_NUM_THREADS"], "4")
        self.assertEqual(first.runtime["version"], "3.8")
        self.assertEqual(
            MultiwfnCommandSpec.from_mapping(first.to_dict()).to_dict(),
            first.to_dict(),
        )
        validate_command_spec(first)

    def test_command_validation_detects_changed_runtime_binary(self) -> None:
        command = build_command_spec(
            runtime=self.runtime,
            source_path=self.source,
            attempt_directory=self.root,
            stdin_path=self.menu,
            outputs=self.outputs,
        )
        self.executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        with self.assertRaisesRegex(MultiwfnAdapterError, "runtime hash") as raised:
            validate_command_spec(command)
        self.assertEqual(raised.exception.code, "RUNTIME_INTEGRITY_MISMATCH")

    def test_missing_outputs_report_roles_and_paths(self) -> None:
        output = self.root / "visualization/inter.cube"
        output.parent.mkdir()
        output.write_text("synthetic\n", encoding="utf-8")

        with self.assertRaises(MultiwfnAdapterError) as raised:
            discover_outputs(self.root, self.outputs)

        report = raised.exception.to_dict()
        self.assertEqual(report["code"], "MISSING_REQUIRED_OUTPUT")
        self.assertEqual(
            set(report["details"]["missing_outputs"]),
            {"sign_lambda2_rho_cube"},
        )


if __name__ == "__main__":
    unittest.main()
