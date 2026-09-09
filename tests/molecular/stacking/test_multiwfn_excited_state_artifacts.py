from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import re
import tempfile
import unittest

from cmw.core.artifacts import (
    ArtifactValidation,
    HoleElectronArtifact,
    NTOArtifact,
    StructureArtifact,
    ValidationStatus,
    artifact_from_dict,
    validate_artifact_compatibility,
)
from cmw.core.execution_layout import ExecutionLayout
from cmw.core.provenance import file_hash
from cmw.molecular.excited_states import (
    StateSelectionResult,
    StateSelectionStatus,
)
from cmw.molecular.multiwfn.excited_states import (
    Multiwfn38HoleElectronRenderer,
    Multiwfn38NtoRenderer,
    Multiwfn2026NtoRenderer,
    MultiwfnFinalizationError,
    finalize_multiwfn_hole_electron_artifact,
    finalize_multiwfn_nto_artifact,
    parse_multiwfn38_hole_electron_session_file,
    parse_multiwfn38_nto_session_file,
    parse_multiwfn2026_nto_session_file,
)
from cmw.molecular.multiwfn.runtime import MENU_CONTRACT
from cmw.molecular.orca.excited_states import parse_orca_tda_excited_states_file
from cmw.molecular.stacking.excited_state import (
    ExcitedStateProtocol,
    create_excited_state_artifact,
)


FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures"
ORCA_ROOT = FIXTURE_ROOT / "orca" / "excited_states"
MULTIWFN_ROOT = FIXTURE_ROOT / "multiwfn" / "excited_states"
PASSED = ArtifactValidation(
    ValidationStatus.PASSED,
    {"fixture": True},
    "VALID_STRUCTURE",
    "fixture source geometry is validated",
)


class MultiwfnArtifactIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cmw multiwfn artifact ")
        self.root = Path(self.temporary.name)
        self.wavefunction = self.root / "source.molden.input"
        self.wavefunction.write_text("[Molden Format]\n", encoding="utf-8")
        self.source = StructureArtifact(
            producing_calculation="fixture-geometry",
            source="fixture.xyz",
            format="xyz",
            atom_count=40,
            elemental_composition={"C": 24, "H": 10, "N": 2, "O": 4},
            charge=0,
            multiplicity=1,
            geometry_hash="b" * 64,
            validation=PASSED,
        )
        self.singlets = parse_orca_tda_excited_states_file(
            ORCA_ROOT / "orca_6_1_1_tda_singlets.out"
        )
        self.mixed = parse_orca_tda_excited_states_file(
            ORCA_ROOT / "orca_6_1_1_tda_mixed.out"
        )
        self.runtime = {
            "program": "Multiwfn",
            "version": "3.8",
            "menu_contract": MENU_CONTRACT,
            "executable": "/fixture/Multiwfn",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _excited_artifact(self, spin: str, index: int):
        parsed = self.singlets if spin == "singlet" else self.mixed
        state = parsed.state(spin, index)
        protocol = ExcitedStateProtocol(
            "WB97X-D3",
            "6-31+G(d,p)",
            30 if spin == "singlet" else 15,
            {"criteria": ("explicit_canonical_state",)},
            tda=True,
            generate_ntos=False,
            functional="WB97X-D3",
            spin_manifold="singlet" if spin == "singlet" else "mixed",
        )
        selection = StateSelectionResult(
            StateSelectionStatus.SELECTED,
            state.identity,
            "explicit_canonical_state",
            {"selected_state": state.identity.to_dict()},
            f"fixture selects canonical state {state.identity.label}",
            1,
            "explicit canonical identity; no tie",
        )
        output = (
            ORCA_ROOT / "orca_6_1_1_tda_singlets.out"
            if spin == "singlet"
            else ORCA_ROOT / "orca_6_1_1_tda_mixed.out"
        )
        artifact = create_excited_state_artifact(
            self.source,
            protocol,
            parsed.states,
            state_selection_rationale=selection.rationale,
            runtime_provenance={"program": "ORCA", "version": "6.1.1"},
            files={"output": str(output)},
            selection_results=(selection,),
            parser_provenance=parsed.source_provenance,
            execution_attempt={"attempt_id": "orca-attempt-001"},
        )
        return artifact, state, output

    def _layout(self, node: str) -> ExecutionLayout:
        layout = ExecutionLayout(
            self.root,
            "pdi",
            node,
            f"{node}-target",
            "attempt_001",
        )
        layout.create_working_directory()
        return layout

    def _historical_session_with_current_wavefunction(self, fixture: str) -> str:
        text = (MULTIWFN_ROOT / fixture).read_text()
        return re.sub(
            r"(?m)^(\s*Loaded\s+).+?(\s+successfully!\s*)$",
            rf"\g<1>{self.wavefunction.resolve()}\g<2>",
            text,
            count=1,
        )

    def _materialize_outputs(self, rendered, session_text: str) -> dict[str, str]:
        working = Path(str(rendered.execution_layout["working_directory"]))
        files: dict[str, str] = {}
        for spec in rendered.outputs:
            path = working / spec.output_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                session_text if spec.role == "session_log" else f"{spec.role}\n",
                encoding="utf-8",
            )
            files[spec.role] = str(path)
        return files

    def test_nto_artifact_finalizes_for_canonical_t2_and_round_trips(self) -> None:
        excited, state, output = self._excited_artifact("triplet", 2)
        layout = self._layout("multiwfn_nto")
        rendered = Multiwfn38NtoRenderer().render(
            state,
            orca_output_path=output,
            source_wavefunction_path=self.wavefunction,
            scientific_protocol_hash=excited.metadata["scientific_protocol_hash"],
            source_geometry_hash=excited.metadata["source_geometry_hash"],
            execution_layout=layout.to_dict(),
            execution_attempt={"attempt_id": "attempt_001"},
        )
        session_text = self._historical_session_with_current_wavefunction(
            "multiwfn_3_8_t2_nto.session.log"
        )
        files = self._materialize_outputs(rendered, session_text)
        parsed = parse_multiwfn38_nto_session_file(
            Path(files["session_log"]),
            expected_state=state,
            output_mwfn_path=files["nto_mwfn"],
            cumulative_weight_cutoff=0.90,
        )

        artifact = finalize_multiwfn_nto_artifact(
            excited,
            rendered,
            parsed,
            process_exit_code=0,
            files=files,
            runtime_provenance=self.runtime,
        )

        self.assertIsInstance(artifact, NTOArtifact)
        self.assertTrue(artifact.validation.passed)
        self.assertEqual(artifact.metadata["selected_state_identity"]["label"], "T2")
        self.assertEqual(
            artifact.metadata["state_source_indices"]["orca_global_state_index"],
            17,
        )
        self.assertEqual(
            artifact.metadata["state_source_indices"]["multiwfn_local_state_index"],
            2,
        )
        self.assertAlmostEqual(artifact.metadata["orbital_pairs"][0]["weight"], 0.496971)
        self.assertEqual(
            artifact.metadata["pair_selection"]["retained_pair_indices"],
            [1, 2],
        )
        validate_artifact_compatibility(artifact, (excited,))
        restored = artifact_from_dict(artifact.to_dict())
        self.assertEqual(restored.artifact_id, artifact.artifact_id)

    def test_nonfragment_hea_artifact_has_no_nto_parent_and_round_trips(self) -> None:
        excited, state, output = self._excited_artifact("singlet", 1)
        layout = self._layout("multiwfn_hea")
        rendered = Multiwfn38HoleElectronRenderer().render(
            state,
            orca_output_path=output,
            source_wavefunction_path=self.wavefunction,
            scientific_protocol_hash=excited.metadata["scientific_protocol_hash"],
            source_geometry_hash=excited.metadata["source_geometry_hash"],
            execution_layout=layout.to_dict(),
            execution_attempt={"attempt_id": "attempt_001"},
        )
        session_text = self._historical_session_with_current_wavefunction(
            "multiwfn_3_8_s1_hea.session.log"
        )
        files = self._materialize_outputs(rendered, session_text)
        parsed = parse_multiwfn38_hole_electron_session_file(
            Path(files["session_log"]),
            expected_state=state,
            expected_grid_quality="medium",
        )

        artifact = finalize_multiwfn_hole_electron_artifact(
            excited,
            rendered,
            parsed,
            process_exit_code=0,
            files=files,
            runtime_provenance=self.runtime,
        )

        self.assertIsInstance(artifact, HoleElectronArtifact)
        self.assertEqual(artifact.parent_artifacts, (excited.artifact_id,))
        self.assertFalse(artifact.metadata["fragment_resolved"])
        self.assertEqual(artifact.metadata["fragment_contributions"], {})
        self.assertEqual(artifact.metadata["reported_D_angstrom"], 0.0)
        self.assertGreater(
            artifact.metadata["derived_D_from_reported_centroids_angstrom"],
            0.0,
        )
        self.assertTrue(artifact.metadata["D_validation"]["consistent"])
        self.assertEqual(artifact.metadata["t_angstrom"], -1.281)
        validate_artifact_compatibility(artifact, (excited,))
        restored = artifact_from_dict(artifact.to_dict())
        self.assertEqual(restored.artifact_id, artifact.artifact_id)

    def test_parser_only_and_missing_output_evidence_cannot_finalize(self) -> None:
        excited, state, output = self._excited_artifact("singlet", 1)
        layout = self._layout("multiwfn_nto_failure")
        rendered = Multiwfn38NtoRenderer().render(
            state,
            orca_output_path=output,
            source_wavefunction_path=self.wavefunction,
            scientific_protocol_hash=excited.metadata["scientific_protocol_hash"],
            source_geometry_hash=excited.metadata["source_geometry_hash"],
            execution_layout=layout.to_dict(),
            execution_attempt={"attempt_id": "attempt_001"},
        )
        session_text = self._historical_session_with_current_wavefunction(
            "multiwfn_3_8_s1_nto.session.log"
        )
        files = self._materialize_outputs(rendered, session_text)
        parsed = parse_multiwfn38_nto_session_file(
            Path(files["session_log"]),
            expected_state=state,
            output_mwfn_path=files["nto_mwfn"],
        )

        with self.assertRaisesRegex(MultiwfnFinalizationError, "zero process"):
            finalize_multiwfn_nto_artifact(
                excited,
                rendered,
                parsed,
                process_exit_code=None,
                files=files,
                runtime_provenance=self.runtime,
            )
        with self.assertRaisesRegex(MultiwfnFinalizationError, "geometry hash"):
            finalize_multiwfn_nto_artifact(
                excited,
                replace(rendered, source_geometry_hash="c" * 64),
                parsed,
                process_exit_code=0,
                files=files,
                runtime_provenance=self.runtime,
            )
        Path(files["nto_mwfn"]).unlink()
        with self.assertRaises(MultiwfnFinalizationError):
            finalize_multiwfn_nto_artifact(
                excited,
                rendered,
                parsed,
                process_exit_code=0,
                files=files,
                runtime_provenance=self.runtime,
            )

    def test_artifact_rejects_cross_state_attachment(self) -> None:
        excited, state, output = self._excited_artifact("triplet", 2)
        layout = self._layout("multiwfn_nto_cross_state")
        rendered = Multiwfn38NtoRenderer().render(
            state,
            orca_output_path=output,
            source_wavefunction_path=self.wavefunction,
            scientific_protocol_hash=excited.metadata["scientific_protocol_hash"],
            source_geometry_hash=excited.metadata["source_geometry_hash"],
            execution_layout=layout.to_dict(),
            execution_attempt={"attempt_id": "attempt_001"},
        )
        s1_excited, s1_state, _ = self._excited_artifact("singlet", 1)
        session_text = self._historical_session_with_current_wavefunction(
            "multiwfn_3_8_s1_nto.session.log"
        )
        files = self._materialize_outputs(rendered, session_text)
        parsed = parse_multiwfn38_nto_session_file(
            Path(files["session_log"]),
            expected_state=s1_state,
        )

        with self.assertRaises(MultiwfnFinalizationError):
            finalize_multiwfn_nto_artifact(
                excited,
                rendered,
                parsed,
                process_exit_code=0,
                files=files,
                runtime_provenance=self.runtime,
            )
        self.assertNotEqual(excited.artifact_id, s1_excited.artifact_id)

    def test_missing_canonical_selection_blocks_both_finalizers(self) -> None:
        excited, state, output = self._excited_artifact("singlet", 1)
        metadata = dict(excited.metadata)
        metadata.pop("selected_state_identities")
        invalid_parent = replace(excited, metadata=metadata)

        nto_layout = self._layout("multiwfn_nto_missing_selection")
        nto_rendered = Multiwfn38NtoRenderer().render(
            state,
            orca_output_path=output,
            source_wavefunction_path=self.wavefunction,
            scientific_protocol_hash=excited.metadata["scientific_protocol_hash"],
            source_geometry_hash=excited.metadata["source_geometry_hash"],
            execution_layout=nto_layout.to_dict(),
            execution_attempt={"attempt_id": "attempt_001"},
        )
        nto_files = self._materialize_outputs(
            nto_rendered,
            self._historical_session_with_current_wavefunction(
                "multiwfn_3_8_s1_nto.session.log"
            ),
        )
        nto_parsed = parse_multiwfn38_nto_session_file(
            Path(nto_files["session_log"]),
            expected_state=state,
            output_mwfn_path=nto_files["nto_mwfn"],
        )
        with self.assertRaisesRegex(
            MultiwfnFinalizationError, "selected_state_identities"
        ):
            finalize_multiwfn_nto_artifact(
                invalid_parent,
                nto_rendered,
                nto_parsed,
                process_exit_code=0,
                files=nto_files,
                runtime_provenance=self.runtime,
            )

        hea_layout = self._layout("multiwfn_hea_missing_selection")
        hea_rendered = Multiwfn38HoleElectronRenderer().render(
            state,
            orca_output_path=output,
            source_wavefunction_path=self.wavefunction,
            scientific_protocol_hash=excited.metadata["scientific_protocol_hash"],
            source_geometry_hash=excited.metadata["source_geometry_hash"],
            execution_layout=hea_layout.to_dict(),
            execution_attempt={"attempt_id": "attempt_001"},
        )
        hea_files = self._materialize_outputs(
            hea_rendered,
            self._historical_session_with_current_wavefunction(
                "multiwfn_3_8_s1_hea.session.log"
            ),
        )
        hea_parsed = parse_multiwfn38_hole_electron_session_file(
            Path(hea_files["session_log"]),
            expected_state=state,
            expected_grid_quality="medium",
        )
        with self.assertRaisesRegex(
            MultiwfnFinalizationError, "selected_state_identities"
        ):
            finalize_multiwfn_hole_electron_artifact(
                invalid_parent,
                hea_rendered,
                hea_parsed,
                process_exit_code=0,
                files=hea_files,
                runtime_provenance=self.runtime,
            )

    def test_2026_artifact_finalizes_through_verified_short_alias_lineage(self) -> None:
        excited, state, output = self._excited_artifact("singlet", 1)
        layout = self._layout("multiwfn_2026_nto")
        settings = self.root / "settings.ini"
        settings.write_text("nthreads= 8\n", encoding="utf-8")
        settings_identity = {
            "settings_path": str(settings.resolve()),
            "settings_sha256": file_hash(settings),
            "settings_source_sha256": "a" * 64,
            "requested_nthreads": 8,
        }
        rendered = Multiwfn2026NtoRenderer().render(
            state,
            orca_output_path=output,
            source_wavefunction_path=self.wavefunction,
            scientific_protocol_hash=excited.metadata["scientific_protocol_hash"],
            source_geometry_hash=excited.metadata["source_geometry_hash"],
            execution_layout=layout.to_dict(),
            execution_attempt={"attempt_id": "attempt_001"},
            settings_identity=settings_identity,
        )
        source_alias = "/tmp/cmw-multiwfn.fixture/source-wavefunction"
        text = (
            MULTIWFN_ROOT / "multiwfn_2026_7_15_s1_nto.session.log"
        ).read_text()
        text = re.sub(
            r"(?m)^(\s*Loaded\s+).+?(\s+successfully!\s*)$",
            rf"\g<1>{source_alias}\g<2>",
            text,
            count=1,
        )
        files = self._materialize_outputs(rendered, text)
        parsed = parse_multiwfn2026_nto_session_file(
            Path(files["session_log"]),
            expected_state=state,
            output_mwfn_path=files["nto_mwfn"],
        )
        manifest = layout.working_directory / "multiwfn-runtime-alias.txt"
        manifest.write_text(
            "settings=/tmp/cmw-multiwfn.fixture/runtime\n"
            f"settings_target={settings.parent.resolve()}\n"
            f"source={source_alias}\n"
            f"source_target={self.wavefunction.resolve()}\n",
            encoding="utf-8",
        )
        runtime = {
            "program": "Multiwfn",
            "version": "2026.7.15",
            "menu_contract": MENU_CONTRACT,
            "executable": "/fixture/Multiwfn",
            **settings_identity,
            "alias_manifest_path": str(manifest),
            "alias_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        }

        artifact = finalize_multiwfn_nto_artifact(
            excited,
            rendered,
            parsed,
            process_exit_code=0,
            files=files,
            runtime_provenance=runtime,
        )

        self.assertTrue(artifact.validation.passed)
        self.assertEqual(artifact.metadata["parser_grammar"], rendered.grammar_id)
        self.assertEqual(
            artifact.metadata["generation_method"],
            "Multiwfn 2026.7.15 natural transition orbitals",
        )
        manifest.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(MultiwfnFinalizationError, "manifest hash"):
            finalize_multiwfn_nto_artifact(
                excited,
                rendered,
                parsed,
                process_exit_code=0,
                files=files,
                runtime_provenance=runtime,
            )


if __name__ == "__main__":
    unittest.main()
