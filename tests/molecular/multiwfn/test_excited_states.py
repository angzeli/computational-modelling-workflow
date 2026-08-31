from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from cmw.core.execution_layout import ExecutionLayout
from cmw.core.provenance import file_hash
from cmw.molecular.multiwfn.adapter import runtime_provenance_with_alias_manifest
from cmw.molecular.multiwfn.excited_states import (
    DeferredFragmentAnalysisError,
    Multiwfn38HoleElectronRenderer,
    Multiwfn38NtoRenderer,
    Multiwfn2026HoleElectronRenderer,
    Multiwfn2026NtoRenderer,
    MultiwfnSessionParseError,
    MultiwfnStateIdentityError,
    ORCA_OUTPUT_LOCAL_PATH,
    UnsupportedMultiwfnFormatError,
    build_excited_state_command_spec,
    parse_multiwfn38_hole_electron_session,
    parse_multiwfn38_hole_electron_session_file,
    parse_multiwfn38_nto_session,
    parse_multiwfn38_nto_session_file,
    parse_multiwfn2026_hole_electron_session,
    parse_multiwfn2026_hole_electron_session_file,
    parse_multiwfn2026_nto_session,
    parse_multiwfn2026_nto_session_file,
)
from cmw.molecular.multiwfn.runtime import MENU_CONTRACT
from cmw.molecular.orca.excited_states import parse_orca_tda_excited_states_file


FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures"
ORCA_ROOT = FIXTURE_ROOT / "orca" / "excited_states"
MULTIWFN_ROOT = FIXTURE_ROOT / "multiwfn" / "excited_states"
RUNTIME_SHELL = Path(__file__).resolve().parents[3] / "scripts/multiwfn/multiwfn_runtime.sh"


class MultiwfnFixtureMixin:
    @classmethod
    def setUpClass(cls) -> None:
        singlets = parse_orca_tda_excited_states_file(
            ORCA_ROOT / "orca_6_1_1_tda_singlets.out"
        )
        mixed = parse_orca_tda_excited_states_file(
            ORCA_ROOT / "orca_6_1_1_tda_mixed.out"
        )
        cls.s1 = singlets.state("singlet", 1)
        cls.t1 = mixed.state("triplet", 1)
        cls.t2 = mixed.state("triplet", 2)


class Multiwfn38FixtureTests(MultiwfnFixtureMixin, unittest.TestCase):
    def test_nto_historical_s1_and_t2_values(self) -> None:
        s1 = parse_multiwfn38_nto_session_file(
            MULTIWFN_ROOT / "multiwfn_3_8_s1_nto.session.log",
            expected_state=self.s1,
            cumulative_weight_cutoff=0.90,
        )
        t2 = parse_multiwfn38_nto_session_file(
            MULTIWFN_ROOT / "multiwfn_3_8_t2_nto.session.log",
            expected_state=self.t2,
            cumulative_weight_cutoff=0.90,
        )

        self.assertEqual(s1.state_evidence.multiwfn_version, "3.8")
        self.assertEqual(s1.state_evidence.selected_identity.label, "S1")
        self.assertEqual(s1.printed_pair_count, 10)
        self.assertAlmostEqual(s1.pairs[0].weight, 0.955480)
        self.assertAlmostEqual(s1.reported_printed_cumulative_weight, 0.991604)
        self.assertEqual(s1.cutoff_selection.retained_pair_indices, (1,))
        self.assertEqual(t2.state_evidence.selected_identity.label, "T2")
        self.assertEqual(t2.state_evidence.selected_local_state_index, 2)
        self.assertEqual(t2.state_evidence.selected_multiplicity, 3)
        self.assertEqual(
            [item.weight for item in t2.pairs[:3]],
            [0.496971, 0.432008, 0.025521],
        )
        self.assertAlmostEqual(t2.reported_printed_cumulative_weight, 0.996808)
        self.assertEqual(t2.cutoff_selection.retained_pair_indices, (1, 2))

    def test_historical_nonfragment_hea_values_and_negative_t(self) -> None:
        cases = (
            (
                "multiwfn_3_8_s1_hea.session.log",
                self.s1,
                (0.86964, 3.622, 3.917, -1.281),
            ),
            (
                "multiwfn_3_8_t1_hea.session.log",
                self.t1,
                (0.88977, 3.588, 3.732, -1.423),
            ),
            (
                "multiwfn_3_8_t2_hea.session.log",
                self.t2,
                (0.90133, 3.688, 3.954, -1.768),
            ),
        )
        for filename, state, expected in cases:
            with self.subTest(filename=filename):
                parsed = parse_multiwfn38_hole_electron_session_file(
                    MULTIWFN_ROOT / filename,
                    expected_state=state,
                    expected_grid_quality="medium",
                )
                self.assertEqual(parsed.state_evidence.selected_identity, state.identity)
                self.assertAlmostEqual(parsed.sr, expected[0])
                self.assertEqual(parsed.reported_D_angstrom, 0.0)
                self.assertAlmostEqual(parsed.hole_extent_angstrom, expected[1])
                self.assertAlmostEqual(parsed.electron_extent_angstrom, expected[2])
                self.assertAlmostEqual(parsed.t_angstrom, expected[3])
                self.assertEqual(parsed.grid_dimensions, (134, 90, 44))
                self.assertEqual(parsed.total_grid_points, 530640)
                self.assertEqual(parsed.coefficient_cross_term_threshold, 0.01000)
                self.assertTrue(parsed.D_validation.consistent)
                self.assertGreater(
                    parsed.derived_D_from_reported_centroids_angstrom,
                    0.0,
                )
                self.assertTrue(parsed.to_metrics().D_consistency["consistent"])

    def test_reduced_fixture_manifest_matches_bytes(self) -> None:
        manifest = json.loads((MULTIWFN_ROOT / "provenance.json").read_text())
        for item in manifest["fixtures"]:
            path = MULTIWFN_ROOT / item["reduced_fixture"]
            data = path.read_bytes()
            self.assertEqual(len(data), item["reduced_fixture_size_bytes"])
            self.assertEqual(
                hashlib.sha256(data).hexdigest(),
                item["reduced_fixture_sha256"],
            )


class Multiwfn38RendererTests(MultiwfnFixtureMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cmw multiwfn es ")
        self.root = Path(self.temporary.name)
        self.layout = ExecutionLayout(
            self.root,
            "pdi",
            "multiwfn_excited_state",
            "state-analysis",
            "attempt_001",
        )
        self.layout.create_working_directory()
        self.wavefunction = self.root / "source.molden.input"
        self.wavefunction.write_text("[Molden Format]\n", encoding="utf-8")
        self.attempt = {"attempt_id": "attempt_001"}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exact_nto_s1_and_t2_menus_and_hashes(self) -> None:
        renderer = Multiwfn38NtoRenderer()
        s1 = renderer.render(
            self.s1,
            orca_output_path=ORCA_ROOT / "orca_6_1_1_tda_singlets.out",
            source_wavefunction_path=self.wavefunction,
            scientific_protocol_hash="protocol-hash",
            source_geometry_hash="b" * 64,
            execution_layout=self.layout.to_dict(),
            execution_attempt=self.attempt,
        )
        t2 = renderer.render(
            self.t2,
            orca_output_path=ORCA_ROOT / "orca_6_1_1_tda_mixed.out",
            source_wavefunction_path=self.wavefunction,
            scientific_protocol_hash="protocol-hash",
            source_geometry_hash="b" * 64,
            execution_layout=self.layout.to_dict(),
            execution_attempt=self.attempt,
        )

        self.assertEqual(
            s1.stdin_text,
            "\n".join(
                (
                    "18",
                    "6",
                    ORCA_OUTPUT_LOCAL_PATH,
                    "1",
                    "3",
                    "S1_nto.mwfn",
                    "0",
                    "0",
                    "q",
                    "",
                )
            ),
        )
        self.assertEqual(t2.menu_sequence[3:5], ("3", "2"))
        self.assertNotIn("17", t2.menu_sequence)
        repeated = renderer.render(
            self.t2,
            orca_output_path=ORCA_ROOT / "orca_6_1_1_tda_mixed.out",
            source_wavefunction_path=self.wavefunction,
            scientific_protocol_hash="protocol-hash",
            source_geometry_hash="b" * 64,
            execution_layout=self.layout.to_dict(),
            execution_attempt=self.attempt,
        )
        self.assertEqual(t2.to_dict(), repeated.to_dict())
        self.assertEqual(t2.stdin_sha256, repeated.stdin_sha256)

    def test_exact_hea_s1_and_t2_menus(self) -> None:
        renderer = Multiwfn38HoleElectronRenderer()
        s1 = renderer.render(
            self.s1,
            orca_output_path=ORCA_ROOT / "orca_6_1_1_tda_singlets.out",
            source_wavefunction_path=self.wavefunction,
            scientific_protocol_hash="protocol-hash",
            source_geometry_hash="b" * 64,
            execution_layout=self.layout.to_dict(),
            execution_attempt=self.attempt,
        )
        t2 = renderer.render(
            self.t2,
            orca_output_path=ORCA_ROOT / "orca_6_1_1_tda_mixed.out",
            source_wavefunction_path=self.wavefunction,
            scientific_protocol_hash="protocol-hash",
            source_geometry_hash="b" * 64,
            execution_layout=self.layout.to_dict(),
            execution_attempt=self.attempt,
        )

        self.assertEqual(
            s1.menu_sequence,
            (
                "18",
                "1",
                ORCA_OUTPUT_LOCAL_PATH,
                "1",
                "1",
                "2",
                "10",
                "1",
                "11",
                "1",
                "0",
                "0",
                "q",
            ),
        )
        self.assertEqual(t2.menu_sequence[3:5], ("3", "2"))
        self.assertNotIn("17", t2.menu_sequence)
        self.assertEqual(t2.settings_metadata["grid_quality"], "medium")

    def test_command_uses_wavefunction_argument_and_materialized_menu(self) -> None:
        rendered = Multiwfn38NtoRenderer().render(
            self.s1,
            orca_output_path=ORCA_ROOT / "orca_6_1_1_tda_singlets.out",
            source_wavefunction_path=self.wavefunction,
            scientific_protocol_hash="protocol-hash",
            source_geometry_hash="b" * 64,
            execution_layout=self.layout.to_dict(),
            execution_attempt=self.attempt,
        )
        executable = self.root / "Multiwfn"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        settings = self.root / "settings.ini"
        settings.write_text("nthreads= 4\n", encoding="utf-8")
        stdin = self.layout.working_directory / "multiwfn.in"
        stdin.write_text(rendered.stdin_text, encoding="utf-8")
        runtime = {
            "executable": str(executable),
            "executable_sha256": file_hash(executable),
            "version": "3.8",
            "menu_contract": MENU_CONTRACT,
            "requested_nthreads": 4,
            "settings_path": str(settings),
            "settings_sha256": file_hash(settings),
        }

        command = build_excited_state_command_spec(
            rendered,
            runtime=runtime,
            attempt_directory=self.layout.working_directory,
            stdin_path=stdin,
        )

        self.assertEqual(command.argv[1], str(self.wavefunction.resolve()))
        self.assertEqual(command.stdin_path, str(stdin.resolve()))
        self.assertEqual(command.outputs, rendered.outputs)
        alias = self.layout.working_directory / ORCA_OUTPUT_LOCAL_PATH
        self.assertTrue(alias.is_symlink())
        self.assertEqual(
            alias.resolve(strict=True),
            (ORCA_ROOT / "orca_6_1_1_tda_singlets.out").resolve(),
        )
        self.assertEqual(
            rendered.auxiliary_inputs[0].source_identity,
            rendered.orca_output_identity,
        )
        self.assertEqual(
            rendered.to_dict()["auxiliary_inputs"][0]["local_path"],
            ORCA_OUTPUT_LOCAL_PATH,
        )

    def test_auxiliary_input_alias_fails_closed_on_conflict(self) -> None:
        rendered = Multiwfn38NtoRenderer().render(
            self.s1,
            orca_output_path=ORCA_ROOT / "orca_6_1_1_tda_singlets.out",
            source_wavefunction_path=self.wavefunction,
            scientific_protocol_hash="protocol-hash",
            source_geometry_hash="b" * 64,
            execution_layout=self.layout.to_dict(),
            execution_attempt=self.attempt,
        )
        executable = self.root / "Multiwfn"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        settings = self.root / "settings.ini"
        settings.write_text("nthreads= 4\n", encoding="utf-8")
        stdin = self.layout.working_directory / "multiwfn.in"
        stdin.write_text(rendered.stdin_text, encoding="utf-8")
        (self.layout.working_directory / ORCA_OUTPUT_LOCAL_PATH).write_text(
            "conflict\n", encoding="utf-8"
        )
        runtime = {
            "executable": str(executable),
            "executable_sha256": file_hash(executable),
            "version": "3.8",
            "menu_contract": MENU_CONTRACT,
            "requested_nthreads": 4,
            "settings_path": str(settings),
            "settings_sha256": file_hash(settings),
        }
        with self.assertRaisesRegex(ValueError, "alias already exists"):
            build_excited_state_command_spec(
                rendered,
                runtime=runtime,
                attempt_directory=self.layout.working_directory,
                stdin_path=stdin,
            )

    def test_short_auxiliary_alias_is_readable_by_launched_multiwfn(self) -> None:
        rendered = Multiwfn38NtoRenderer().render(
            self.s1,
            orca_output_path=ORCA_ROOT / "orca_6_1_1_tda_singlets.out",
            source_wavefunction_path=self.wavefunction,
            scientific_protocol_hash="protocol-hash",
            source_geometry_hash="b" * 64,
            execution_layout=self.layout.to_dict(),
            execution_attempt=self.attempt,
        )
        executable = self.root / "fake Multiwfn"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "lines = sys.stdin.read().splitlines()\n"
            "print('AUX=' + lines[2])\n"
            "print('AUX_EXISTS=' + str(pathlib.Path(lines[2]).is_file()))\n"
            "print('SOURCE=' + sys.argv[1])\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        settings_directory = self.root / "runtime settings"
        settings_directory.mkdir()
        settings = settings_directory / "settings.ini"
        settings.write_text("nthreads= 4\n", encoding="utf-8")
        stdin = self.layout.working_directory / "multiwfn.in"
        stdin.write_text(rendered.stdin_text, encoding="utf-8")
        runtime = {
            "executable": str(executable),
            "executable_sha256": file_hash(executable),
            "version": "3.8",
            "menu_contract": MENU_CONTRACT,
            "requested_nthreads": 4,
            "settings_path": str(settings),
            "settings_sha256": file_hash(settings),
        }
        build_excited_state_command_spec(
            rendered,
            runtime=runtime,
            attempt_directory=self.layout.working_directory,
            stdin_path=stdin,
        )
        completed = subprocess.run(
            (
                "bash",
                "-c",
                'set -euo pipefail; source "$1"; MULTIWFN_EXE="$2"; '
                'MULTIWFN_NTHREADS=4; MULTIWFN_RUN_SETTINGS_PATH="$3"; '
                'MULTIWFN_RUN_SETTINGS_DIRECTORY=${MULTIWFN_RUN_SETTINGS_PATH%/settings.ini}; '
                'cd "$4"; multiwfn_runtime_launch "$5" < "$6"',
                "_",
                str(RUNTIME_SHELL),
                str(executable),
                str(settings),
                str(self.layout.working_directory),
                str(self.wavefunction),
                str(stdin),
            ),
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            check=True,
        )
        values = dict(
            line.split("=", 1) for line in completed.stdout.splitlines()
        )
        self.assertEqual(values["AUX"], ORCA_OUTPUT_LOCAL_PATH)
        self.assertEqual(values["AUX_EXISTS"], "True")
        self.assertLess(len(values["SOURCE"]), 160)
        self.assertNotIn(" ", values["SOURCE"])
        provenance = runtime_provenance_with_alias_manifest(
            runtime, self.layout.working_directory
        )
        manifest = self.layout.working_directory / "multiwfn-runtime-alias.txt"
        self.assertEqual(provenance["alias_manifest_path"], str(manifest.resolve()))
        self.assertEqual(provenance["alias_manifest_sha256"], file_hash(manifest))

    def test_fragment_request_is_explicitly_deferred(self) -> None:
        with self.assertRaises(DeferredFragmentAnalysisError) as raised:
            Multiwfn38HoleElectronRenderer().render(
                self.s1,
                orca_output_path=ORCA_ROOT / "orca_6_1_1_tda_singlets.out",
                source_wavefunction_path=self.wavefunction,
                scientific_protocol_hash="protocol-hash",
                source_geometry_hash="b" * 64,
                execution_layout=self.layout.to_dict(),
                execution_attempt=self.attempt,
                fragment_definitions=({"fragment": "A"},),
            )
        self.assertEqual(raised.exception.code, "DEFERRED_FRAGMENT_RESOLVED_HEA")


class Multiwfn38NegativeParserTests(MultiwfnFixtureMixin, unittest.TestCase):
    def test_nto_rejects_version_selection_weight_and_completion_failures(self) -> None:
        text = (MULTIWFN_ROOT / "multiwfn_3_8_s1_nto.session.log").read_text()
        cases = (
            (
                text.replace("Version 3.8", "Version 3.7", 1),
                UnsupportedMultiwfnFormatError,
            ),
            (
                text.replace(
                    "Loading configuration coefficients of excited state   1...",
                    "Configuration unavailable",
                    1,
                ),
                MultiwfnSessionParseError,
            ),
            (
                text.replace(
                    "    0.001108    0.000918    0.000679    0.000613    0.000609\n",
                    "",
                    1,
                ),
                MultiwfnSessionParseError,
            ),
            (
                text.replace("Exporting .mwfn file finished!", "Export interrupted", 1),
                MultiwfnSessionParseError,
            ),
        )
        for changed, exception in cases:
            with self.subTest(exception=exception.__name__):
                with self.assertRaises(exception):
                    parse_multiwfn38_nto_session(
                        changed,
                        expected_state=self.s1,
                    )

    def test_nto_rejects_inconsistent_expected_state(self) -> None:
        text = (MULTIWFN_ROOT / "multiwfn_3_8_s1_nto.session.log").read_text()
        with self.assertRaises(MultiwfnStateIdentityError):
            parse_multiwfn38_nto_session(text, expected_state=self.t2)

    def test_hea_rejects_descriptor_grid_centroid_and_D_failures(self) -> None:
        text = (MULTIWFN_ROOT / "multiwfn_3_8_s1_hea.session.log").read_text()
        changed_values = (
            text.replace("Sr index", "Missing Sr index", 1),
            text.replace("Centroid of hole", "Malformed hole center", 1),
            text.replace("Total:      530640", "Total:      530641", 1),
            text.replace("D index:   0.000", "D index:   2.000", 1),
        )
        for changed in changed_values:
            with self.subTest(changed=hash(changed)):
                with self.assertRaises(MultiwfnSessionParseError):
                    parse_multiwfn38_hole_electron_session(
                        changed,
                        expected_state=self.s1,
                        expected_grid_quality="medium",
                    )


class Multiwfn2026FixtureTests(MultiwfnFixtureMixin, unittest.TestCase):
    def test_real_2026_nto_fixtures_match_historical_values(self) -> None:
        s1 = parse_multiwfn2026_nto_session_file(
            MULTIWFN_ROOT / "multiwfn_2026_7_15_s1_nto.session.log",
            expected_state=self.s1,
            cumulative_weight_cutoff=0.90,
        )
        t2 = parse_multiwfn2026_nto_session_file(
            MULTIWFN_ROOT / "multiwfn_2026_7_15_t2_nto.session.log",
            expected_state=self.t2,
            cumulative_weight_cutoff=0.90,
        )
        historical_s1 = parse_multiwfn38_nto_session_file(
            MULTIWFN_ROOT / "multiwfn_3_8_s1_nto.session.log",
            expected_state=self.s1,
        )
        historical_t2 = parse_multiwfn38_nto_session_file(
            MULTIWFN_ROOT / "multiwfn_3_8_t2_nto.session.log",
            expected_state=self.t2,
        )

        self.assertEqual(s1.state_evidence.multiwfn_version, "2026.7.15")
        self.assertEqual(s1.state_evidence.parallel_threads, 8)
        self.assertEqual(s1.state_evidence.selected_identity.label, "S1")
        self.assertEqual(t2.state_evidence.selected_identity.label, "T2")
        self.assertEqual(t2.state_evidence.selected_local_state_index, 2)
        self.assertNotEqual(t2.state_evidence.selected_local_state_index, 17)
        self.assertEqual(
            [item.weight for item in s1.pairs],
            [item.weight for item in historical_s1.pairs],
        )
        self.assertEqual(
            [item.weight for item in t2.pairs],
            [item.weight for item in historical_t2.pairs],
        )
        self.assertEqual(s1.reported_printed_cumulative_weight, 0.991604)
        self.assertEqual(t2.reported_printed_cumulative_weight, 0.996808)

    def test_real_2026_hea_fixtures_match_historical_values(self) -> None:
        cases = (
            ("s1", self.s1, 0.86964, 3.622, 3.917, -1.281),
            ("t2", self.t2, 0.90133, 3.688, 3.954, -1.768),
        )
        for label, state, sr, hole, electron, t_value in cases:
            with self.subTest(state=label):
                current = parse_multiwfn2026_hole_electron_session_file(
                    MULTIWFN_ROOT / f"multiwfn_2026_7_15_{label}_hea.session.log",
                    expected_state=state,
                    expected_grid_quality="medium",
                )
                historical = parse_multiwfn38_hole_electron_session_file(
                    MULTIWFN_ROOT / f"multiwfn_3_8_{label}_hea.session.log",
                    expected_state=state,
                    expected_grid_quality="medium",
                )
                self.assertEqual(current.state_evidence.multiwfn_version, "2026.7.15")
                self.assertEqual(current.state_evidence.parallel_threads, 8)
                self.assertEqual(current.sr, sr)
                self.assertEqual(current.hole_extent_angstrom, hole)
                self.assertEqual(current.electron_extent_angstrom, electron)
                self.assertEqual(current.t_angstrom, t_value)
                self.assertEqual(current.grid_dimensions, (134, 90, 44))
                self.assertEqual(current.total_grid_points, 530640)
                self.assertEqual(current.coefficient_cross_term_threshold, 0.01)
                self.assertEqual(current.sr, historical.sr)
                self.assertEqual(current.reported_D_angstrom, historical.reported_D_angstrom)
                self.assertEqual(current.hole_extent_angstrom, historical.hole_extent_angstrom)
                self.assertEqual(current.electron_extent_angstrom, historical.electron_extent_angstrom)
                self.assertEqual(current.t_angstrom, historical.t_angstrom)

    def test_exact_parser_rejects_future_and_cross_version_grammar(self) -> None:
        text = (MULTIWFN_ROOT / "multiwfn_2026_7_15_s1_nto.session.log").read_text()
        with self.assertRaises(UnsupportedMultiwfnFormatError):
            parse_multiwfn2026_nto_session(
                text.replace("2026.7.15", "2026.7.16", 1),
                expected_state=self.s1,
            )
        with self.assertRaises(UnsupportedMultiwfnFormatError):
            parse_multiwfn38_nto_session(text, expected_state=self.s1)
        hea = (MULTIWFN_ROOT / "multiwfn_2026_7_15_s1_hea.session.log").read_text()
        with self.assertRaises(UnsupportedMultiwfnFormatError):
            parse_multiwfn38_hole_electron_session(
                hea,
                expected_state=self.s1,
                expected_grid_quality="medium",
            )


class Multiwfn2026RendererTests(MultiwfnFixtureMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cmw multiwfn 2026 ")
        self.root = Path(self.temporary.name)
        self.layout = ExecutionLayout(
            self.root,
            "pdi",
            "multiwfn_excited_state",
            "state-analysis",
            "attempt_001",
        )
        self.layout.create_working_directory()
        self.wavefunction = self.root / "source.molden.input"
        self.wavefunction.write_text("[Molden Format]\n", encoding="utf-8")
        self.settings = self.root / "settings.ini"
        self.settings.write_text("nthreads= 8\n", encoding="utf-8")
        self.settings_identity = {
            "settings_path": str(self.settings.resolve()),
            "settings_sha256": file_hash(self.settings),
            "settings_source_sha256": "a" * 64,
            "requested_nthreads": 8,
        }
        self.attempt = {"attempt_id": "attempt_001"}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _render(self, renderer, state, output):
        return renderer.render(
            state,
            orca_output_path=ORCA_ROOT / output,
            source_wavefunction_path=self.wavefunction,
            scientific_protocol_hash="protocol-hash",
            source_geometry_hash="b" * 64,
            execution_layout=self.layout.to_dict(),
            execution_attempt=self.attempt,
            settings_identity=self.settings_identity,
        )

    def test_exact_nto_s1_and_t2_menus(self) -> None:
        renderer = Multiwfn2026NtoRenderer()
        s1 = self._render(renderer, self.s1, "orca_6_1_1_tda_singlets.out")
        t2 = self._render(renderer, self.t2, "orca_6_1_1_tda_mixed.out")
        self.assertEqual(
            s1.menu_sequence,
            (
                "18", "6", ORCA_OUTPUT_LOCAL_PATH,
                "1", "3", "S1_nto.mwfn", "0", "0", "q",
            ),
        )
        self.assertEqual(t2.menu_sequence[3:5], ("3", "2"))
        self.assertNotIn("17", t2.menu_sequence)
        self.assertEqual(t2.grammar_id, "multiwfn_2026_7_15_nto_v1")
        self.assertEqual(t2.to_dict(), self._render(renderer, self.t2, "orca_6_1_1_tda_mixed.out").to_dict())

    def test_exact_hea_s1_and_t2_menus(self) -> None:
        renderer = Multiwfn2026HoleElectronRenderer()
        s1 = self._render(renderer, self.s1, "orca_6_1_1_tda_singlets.out")
        t2 = self._render(renderer, self.t2, "orca_6_1_1_tda_mixed.out")
        self.assertEqual(s1.menu_sequence[-4:], ("0", "0", "0", "q"))
        self.assertEqual(t2.menu_sequence[3:5], ("3", "2"))
        self.assertNotIn("17", t2.menu_sequence)
        self.assertEqual(t2.grammar_id, "multiwfn_2026_7_15_nonfragment_hea_v1")

    def test_settings_identity_is_required_and_command_must_match_it(self) -> None:
        with self.assertRaisesRegex(ValueError, "settings identity"):
            Multiwfn2026NtoRenderer().render(
                self.s1,
                orca_output_path=ORCA_ROOT / "orca_6_1_1_tda_singlets.out",
                source_wavefunction_path=self.wavefunction,
                scientific_protocol_hash="protocol-hash",
                source_geometry_hash="b" * 64,
                execution_layout=self.layout.to_dict(),
                execution_attempt=self.attempt,
            )
        rendered = self._render(
            Multiwfn2026NtoRenderer(),
            self.s1,
            "orca_6_1_1_tda_singlets.out",
        )
        executable = self.root / "Multiwfn"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        stdin = self.layout.working_directory / "multiwfn.in"
        stdin.write_text(rendered.stdin_text, encoding="utf-8")
        runtime = {
            "executable": str(executable),
            "executable_sha256": file_hash(executable),
            "version": "2026.7.15",
            "menu_contract": MENU_CONTRACT,
            **self.settings_identity,
        }
        command = build_excited_state_command_spec(
            rendered,
            runtime=runtime,
            attempt_directory=self.layout.working_directory,
            stdin_path=stdin,
        )
        self.assertEqual(command.argv[1], str(self.wavefunction.resolve()))
        with self.assertRaisesRegex(ValueError, "settings_sha256"):
            build_excited_state_command_spec(
                rendered,
                runtime={**runtime, "settings_sha256": "c" * 64},
                attempt_directory=self.layout.working_directory,
                stdin_path=stdin,
            )


if __name__ == "__main__":
    unittest.main()
