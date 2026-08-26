from __future__ import annotations

from dataclasses import replace
import math
import tempfile
import unittest
from pathlib import Path

from cmw.core.artifacts import (
    ExcitedStateArtifact,
    HoleElectronArtifact,
    NTOArtifact,
    artifact_from_dict,
)
from cmw.core.execution_profiles import (
    ExecutionProfile,
    MultiwfnResourcePolicy,
    OrcaResourcePolicy,
)
from cmw.core.plan_materialization import (
    WorkflowPlanMaterializationError,
    WorkflowPlanMaterializer,
)
from cmw.core.provenance import file_hash
from cmw.core.structure_artifacts import structure_artifact_from_file
from cmw.molecular.multiwfn.adapter import MultiwfnOutputSpec
from cmw.molecular.orca.protocol import (
    ProtocolIntent,
    ProtocolValidationStatus,
    validate_protocol,
)
from cmw.molecular.orca.renderer import ORCA_RENDERER_ID, OrcaExecutionRenderer
from cmw.molecular.orca.status import (
    ScientificStatus,
    StageType,
    parse_orca_output,
    validate_stage,
)
from cmw.molecular.stacking import (
    DeferredStateSelectionError,
    ExcitedStateContractError,
    ExcitedStateProtocol,
    ExcitedStateRecord,
    FixedRegion,
    FragmentDefinition,
    GroundStateProtocol,
    HoleElectronMetrics,
    HoleElectronProtocol,
    NTOOrbitalRecord,
    PeriodicPairDefinition,
    RelaxationContractError,
    RelaxationProtocol,
    StackingAssemblyError,
    StackingRegistryError,
    StackingWorkflowValidationError,
    VerticalStackingProtocol,
    assemble_vertical_dimer,
    build_hole_electron_command_spec,
    build_relaxation_stage_specs,
    build_vertical_stacking_workflow,
    create_excited_state_artifact,
    create_hole_electron_artifact,
    create_nto_artifact,
    dimer_structure_artifact_from_file,
    extract_stacking_template,
    plan_hole_electron_analysis,
    prepare_vertical_stacking_workflow,
    render_orca_fixed_region_block,
    stacking_execution_plan,
    validate_dimer_structure,
    validate_excited_state_artifact,
    validate_hole_electron_artifact,
    validate_nto_artifact,
    validate_relaxation_protocol,
    validate_stacking_artifact_lineage,
    validate_stacking_template,
)


MONOMER_XYZ = """4
generic planar unit
C  0.000000000000  0.000000000000  0.000000000000
C  1.000000000000  0.000000000000  0.000000000000
C  0.000000000000  1.000000000000  0.000000000000
H -1.000000000000  0.000000000000  0.000000000000
"""


def periodic_xyz(angle_degrees: float = 30.0) -> str:
    angle = math.radians(angle_degrees)
    cosine, sine = math.cos(angle), math.sin(angle)

    def transformed(x: float, y: float) -> tuple[float, float, float]:
        return (
            0.5 + cosine * x - sine * y,
            0.25 + sine * x + cosine * y,
            3.4,
        )

    rows = [
        "8",
        "synthetic periodic molecular pair",
        "C 0.0 0.0 0.0",
        "C 1.0 0.0 0.0",
        "C 0.0 1.0 0.0",
        "H -1.0 0.0 0.0",
    ]
    for element, x, y in (
        ("C", 0.0, 0.0),
        ("C", 1.0, 0.0),
        ("C", 0.0, 1.0),
        ("H", -1.0, 0.0),
    ):
        tx, ty, tz = transformed(x, y)
        rows.append(f"{element} {tx:.12f} {ty:.12f} {tz:.12f}")
    return "\n".join(rows) + "\n"


class StackingFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.periodic_path = self.root / "periodic.xyz"
        self.monomer_path = self.root / "monomer.xyz"
        self.periodic_path.write_text(periodic_xyz(), encoding="utf-8")
        self.monomer_path.write_text(MONOMER_XYZ, encoding="utf-8")
        self.periodic = structure_artifact_from_file(
            self.periodic_path,
            source="synthetic periodic structure",
            metadata={
                "periodic": True,
                "periodic_origin": {
                    "cell": "synthetic",
                    "image_selection": "explicit_pair",
                },
            },
        )
        self.monomer = structure_artifact_from_file(
            self.monomer_path,
            source="synthetic monomer",
            metadata={"molecule_id": "generic_unit"},
        )
        self.pair = PeriodicPairDefinition(
            ("generic_unit", "generic_unit"),
            {"A": (0, 1, 2, 3), "B": (4, 5, 6, 7)},
            {"A": (0, 1, 2), "B": (4, 5, 6)},
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def template(self):
        return extract_stacking_template(
            self.periodic,
            self.pair,
            extraction_method="ordered_core_plane_fit_v1",
        )

    def dimer(self):
        return assemble_vertical_dimer(
            self.monomer,
            self.template(),
            self.root / "assembled.xyz",
        )

    def stacking_protocol(self) -> VerticalStackingProtocol:
        relaxation = RelaxationProtocol(
            (
                FixedRegion("core_A", (0, 1, 2)),
                FixedRegion("core_B", (4, 5, 6)),
            )
        )
        ground = GroundStateProtocol("PBE0", "def2-SVP", frequency=True)
        excited = ExcitedStateProtocol(
            "PBE0",
            "def2-SVP",
            3,
            {"states": [1, 2], "criterion": "explicit"},
            tda=True,
            visualization={"isovalue": 0.03},
            functional="PBE0",
        )
        hole = HoleElectronProtocol(
            1,
            "explicit-test-menu-v1",
            ("18", "1", "1", "0", "q"),
            (
                MultiwfnOutputSpec(
                    "hole_density", "hole.cub", "analysis/hole.cub"
                ),
                MultiwfnOutputSpec(
                    "electron_density", "electron.cub", "analysis/electron.cub"
                ),
                MultiwfnOutputSpec(
                    "metrics", "hole_electron.txt", "analysis/metrics.txt",
                    media_type="text/plain",
                ),
            ),
            {"format": "cube"},
            (
                FragmentDefinition("fragment_1", (0, 1, 2, 3)),
                FragmentDefinition("fragment_2", (4, 5, 6, 7)),
            ),
        )
        return VerticalStackingProtocol(relaxation, ground, ground, excited, hole)

    def excited_artifact(self, source=None) -> ExcitedStateArtifact:
        selected_source = source or self.dimer()
        output = self.root / f"{selected_source.artifact_id[:8]}-tddft.out"
        output.write_text("synthetic TDDFT evidence\n", encoding="utf-8")
        return create_excited_state_artifact(
            selected_source,
            self.stacking_protocol().excited_state,
            (
                ExcitedStateRecord(
                    1,
                    2.40,
                    0.18,
                    "singlet",
                    ("lowest_bright",),
                    "first state above the declared oscillator-strength threshold",
                ),
                ExcitedStateRecord(
                    2,
                    2.75,
                    0.01,
                    "singlet",
                    ("lowest_ct_like", "experimentally_relevant"),
                    "selected by a declared fragment-transfer criterion",
                ),
            ),
            state_selection_rationale=(
                "retain the lowest bright and lowest declared transfer-like states"
            ),
            runtime_provenance={"program": "ORCA", "version": "synthetic-6.1"},
            files={"output": str(output)},
            producing_calculation="synthetic_tddft",
        )

    def nto_artifact(self, excited: ExcitedStateArtifact) -> NTOArtifact:
        files: dict[str, str] = {}
        pairs = []
        for state_index in (1, 2):
            hole_role = f"state_{state_index}_hole"
            electron_role = f"state_{state_index}_electron"
            for role in (hole_role, electron_role):
                path = self.root / f"{role}.cube"
                path.write_text("synthetic orbital evidence\n", encoding="utf-8")
                files[role] = str(path)
            pairs.append(
                NTOOrbitalRecord(
                    state_index,
                    1,
                    0.90,
                    hole_role,
                    electron_role,
                )
            )
        return create_nto_artifact(
            excited,
            pairs,
            generation_method="ORCA TDDFT natural transition orbitals",
            runtime_provenance={"program": "ORCA", "version": "synthetic-6.1"},
            files=files,
            visualization={"isovalue": 0.03},
            producing_calculation="synthetic_nto",
        )


class TemplateAndAssemblyTests(StackingFixture):
    def test_extracts_valid_periodic_registry_with_stable_provenance(self) -> None:
        template = self.template()

        self.assertTrue(template.validation.passed)
        self.assertTrue(validate_stacking_template(template, source=self.periodic).passed)
        geometry = template.metadata["geometry"]
        self.assertAlmostEqual(geometry["plane_separation_angstrom"], 3.4, places=8)
        self.assertEqual(
            template.metadata["core_definition"]["local_atom_indices"]["B"],
            [0, 1, 2],
        )
        self.assertEqual(template.provenance["source_hash"], self.periodic.geometry_hash)

        restored = artifact_from_dict(template.to_dict())
        self.assertEqual(restored.artifact_id, template.artifact_id)
        self.assertEqual(restored.artifact_type, "StackingTemplateArtifact")

    def test_invalid_core_mapping_and_nonperiodic_source_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside its molecule"):
            PeriodicPairDefinition(
                ("generic_unit", "generic_unit"),
                {"A": (0, 1, 2, 3), "B": (4, 5, 6, 7)},
                {"A": (0, 1, 4), "B": (4, 5, 6)},
            )

        nonperiodic = replace(
            self.periodic,
            metadata={"periodic": False},
        )
        with self.assertRaisesRegex(StackingRegistryError, "periodic: true"):
            extract_stacking_template(
                nonperiodic,
                self.pair,
                extraction_method="ordered_core_plane_fit_v1",
            )

    def test_assembly_preserves_atoms_provenance_and_registry(self) -> None:
        template = self.template()
        dimer = assemble_vertical_dimer(
            self.monomer,
            template,
            self.root / "assembled.xyz",
        )

        self.assertEqual(dimer.atom_count, 8)
        self.assertEqual(dimer.elemental_composition, {"C": 6, "H": 2})
        self.assertTrue(dimer.validation.passed)
        self.assertTrue(
            validate_dimer_structure(
                dimer, parents=(self.monomer, template)
            ).passed
        )
        self.assertEqual(dimer.metadata["components"]["A"]["atom_indices"], [0, 1, 2, 3])
        self.assertEqual(dimer.metadata["components"]["B"]["atom_indices"], [4, 5, 6, 7])
        self.assertEqual(dimer.provenance["stacking_template"], template.artifact_id)
        self.assertEqual(
            dimer.metadata["stacking_registry"]["template_artifact"],
            template.artifact_id,
        )

        restored = artifact_from_dict(dimer.to_dict())
        self.assertEqual(restored.artifact_id, dimer.artifact_id)
        self.assertEqual(restored.artifact_type, "DimerStructureArtifact")

    def test_assembly_rejects_mismatched_monomer_identity(self) -> None:
        wrong = replace(self.monomer, metadata={"molecule_id": "other"})
        with self.assertRaisesRegex(StackingAssemblyError, "do not match template"):
            assemble_vertical_dimer(
                wrong,
                self.template(),
                self.root / "not-written.xyz",
            )

    def test_preassembled_dimer_preserves_identity_and_registry(self) -> None:
        template = self.template()
        assembled = assemble_vertical_dimer(
            self.monomer,
            template,
            self.root / "assembled-source.xyz",
        )
        imported = dimer_structure_artifact_from_file(
            self.monomer,
            template,
            self.root / "assembled-source.xyz",
            preprocessing_provenance={
                "protocol": "synthetic-v1",
                "candidate_id": 7,
            },
        )

        self.assertEqual(imported.geometry_hash, assembled.geometry_hash)
        self.assertEqual(imported.parent_artifacts, assembled.parent_artifacts)
        self.assertEqual(imported.metadata["components"], assembled.metadata["components"])
        self.assertEqual(imported.provenance["preprocessing"]["candidate_id"], 7)
        self.assertTrue(
            validate_dimer_structure(
                imported, parents=(self.monomer, self.monomer, template)
            ).passed
        )


class RelaxationTests(StackingFixture):
    def test_constraints_are_applied_only_to_the_controlled_stage(self) -> None:
        protocol = self.stacking_protocol()
        dimer = self.dimer()
        validate_relaxation_protocol(protocol.relaxation, atom_count=dimer.atom_count)
        block = render_orca_fixed_region_block(protocol.relaxation)
        stages = build_relaxation_stage_specs(
            protocol.relaxation, protocol.relaxation_method
        )

        self.assertIn("{ C 0 C }", block)
        self.assertIn("{ C 6 C }", block)
        self.assertEqual(stages["constrained_optimization"].stage_type, StageType.OPT)
        self.assertTrue(stages["constrained_optimization"].blocks)
        self.assertFalse(stages["full_optimization"].blocks)
        self.assertEqual(
            stages["full_optimization"].protocol["constraints"]["fixed_regions"],
            [],
        )

    def test_constraints_outside_structure_fail_closed(self) -> None:
        relaxation = RelaxationProtocol((FixedRegion("invalid", (10,)),))
        with self.assertRaisesRegex(RelaxationContractError, "exceeds"):
            validate_relaxation_protocol(relaxation, atom_count=8)

    def test_composite_method_and_separate_frequency_protocol(self) -> None:
        relaxation = GroundStateProtocol("r2SCAN-3c", "")
        frequency = GroundStateProtocol(
            "r2SCAN-3c",
            "",
            frequency=True,
            protocol={"maximum_imaginary_modes": 0},
        )
        protocol = self.stacking_protocol()
        protocol = VerticalStackingProtocol(
            protocol.relaxation,
            relaxation,
            GroundStateProtocol("wB97X-D4", "def2-TZVPP"),
            protocol.excited_state,
            protocol.hole_electron,
            frequency_method=frequency,
        )

        plan = prepare_vertical_stacking_workflow(
            periodic_source=self.periodic,
            pair=self.pair,
            monomer_a=self.monomer,
            dimer_output_path=self.root / "composite-method-dimer.xyz",
            protocol=protocol,
            extraction_method="ordered_core_plane_fit_v1",
        )

        self.assertEqual(relaxation.keyword_line, "r2SCAN-3c")
        self.assertEqual(
            plan.orca_plans["constrained_optimization"].spec.keywords,
            "r2SCAN-3c",
        )
        self.assertEqual(
            plan.orca_plans["frequency"].spec.keywords,
            "r2SCAN-3c",
        )
        self.assertEqual(
            plan.orca_plans["ground_state"].spec.keywords,
            "wB97X-D4 def2-TZVPP",
        )


class ExcitedStateAndAnalysisTests(StackingFixture):
    def test_excited_state_protocol_and_artifact_metadata_are_explicit(self) -> None:
        protocol = self.stacking_protocol().excited_state
        stage = protocol.orca_stage_spec()
        self.assertEqual(stage.stage_type, StageType.TDDFT)
        self.assertEqual(stage.execution_intent.task.value, "excited_state")
        self.assertEqual(stage.execution_intent.required_behavior, "SP")
        self.assertIn("nroots 3", stage.blocks[0])
        self.assertIn("tda true", stage.blocks[0])
        self.assertIn("DoNTO true", stage.blocks[0])
        self.assertIn("NTOStates 1,2", stage.blocks[0])

        artifact = self.excited_artifact(self.monomer)
        validation = validate_excited_state_artifact(
            artifact, source=self.monomer, protocol=protocol
        )
        self.assertTrue(validation.passed)
        self.assertEqual(artifact.metadata["functional"], "PBE0")
        self.assertEqual(artifact.metadata["program"], "ORCA")
        self.assertEqual(artifact.metadata["program_version"], "synthetic-6.1")
        self.assertEqual(artifact.metadata["selected_state_indices"], [1, 2])
        self.assertEqual(
            artifact.metadata["excited_states"][0]["oscillator_strength"],
            0.18,
        )
        self.assertIn("lowest_bright", artifact.metadata["excited_states"][0]["selection_labels"])
        restored = artifact_from_dict(artifact.to_dict())
        self.assertEqual(restored.artifact_id, artifact.artifact_id)
        self.assertEqual(
            restored.metadata["state_selection_rationale"],
            artifact.metadata["state_selection_rationale"],
        )

        incomplete = replace(
            artifact,
            metadata={
                key: value
                for key, value in artifact.metadata.items()
                if key != "excited_states"
            },
        )
        self.assertFalse(
            validate_excited_state_artifact(
                incomplete,
                source=self.monomer,
                protocol=protocol,
            ).passed
        )

        with self.assertRaisesRegex(ExcitedStateContractError, "within number_of_roots"):
            ExcitedStateProtocol("PBE0", "def2-SVP", 2, {"states": [3]})

    def test_excited_state_artifact_fails_without_selection_evidence(self) -> None:
        output = self.root / "incomplete-tddft.out"
        output.write_text("synthetic TDDFT evidence\n", encoding="utf-8")
        with self.assertRaisesRegex(
            ExcitedStateContractError, "rationale is required"
        ):
            create_excited_state_artifact(
                self.monomer,
                self.stacking_protocol().excited_state,
                (
                    ExcitedStateRecord(1, 2.4, 0.18, "singlet"),
                    ExcitedStateRecord(2, 2.7, 0.01, "singlet"),
                ),
                state_selection_rationale="",
                runtime_provenance={
                    "program": "ORCA",
                    "version": "synthetic-6.1",
                },
                files={"output": str(output)},
            )

    def test_tddft_output_and_protocol_validation_fail_closed(self) -> None:
        valid_output = """INPUT FILE
| 1> ! PBE0 def2-SVP SP
| 2> %tddft
| 3> nroots 2
| 4> end
| 5> ****END OF INPUT****
SCF CONVERGED AFTER 8 CYCLES
FINAL SINGLE POINT ENERGY      -100.123456
TD-DFT/TDA EXCITED STATES (SINGLETS)
STATE   1: E= 0.100 au
STATE   2: E= 0.200 au
ORCA TERMINATED NORMALLY
"""
        evidence = parse_orca_output(valid_output)
        self.assertEqual(evidence.excited_state_count, 2)
        self.assertEqual(
            validate_stage(evidence, StageType.TDDFT).status,
            ScientificStatus.VALID,
        )
        protocol = validate_protocol(
            evidence,
            ProtocolIntent(
                method="PBE0",
                basis="def2-SVP",
                excited_state_required=True,
                minimum_excited_states=2,
            ),
            StageType.TDDFT,
        )
        self.assertEqual(protocol.status, ProtocolValidationStatus.PASSED)

        missing = parse_orca_output(
            valid_output.replace("STATE   2: E= 0.200 au\n", "")
        )
        failed = validate_protocol(
            missing,
            ProtocolIntent(minimum_excited_states=2),
            StageType.TDDFT,
        )
        self.assertEqual(
            failed.status,
            ProtocolValidationStatus.FAILED_PROTOCOL_MISMATCH,
        )

    def test_hole_electron_artifact_records_lineage_metrics_and_runtime(self) -> None:
        stacking_protocol = self.stacking_protocol()
        dimer = self.dimer()
        excited = self.excited_artifact(dimer)
        nto = self.nto_artifact(excited)
        self.assertTrue(validate_nto_artifact(nto, excited_state=excited).passed)
        restored_nto = artifact_from_dict(nto.to_dict())
        self.assertEqual(restored_nto.artifact_id, nto.artifact_id)
        self.assertEqual(
            restored_nto.metadata["orbital_pairs"],
            nto.metadata["orbital_pairs"],
        )
        metrics = HoleElectronMetrics(
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 2.1),
            2.1,
            0.35,
            0.65,
            {
                "fragment_1": {"hole": 0.8, "electron": 0.2},
                "fragment_2": {"hole": 0.2, "electron": 0.8},
            },
            1.2,
            1.4,
        )
        files = {
            item.role: str(self.root / item.output_path)
            for item in stacking_protocol.hole_electron.outputs
        }
        for path in files.values():
            selected = Path(path)
            selected.parent.mkdir(parents=True, exist_ok=True)
            selected.write_text("synthetic output\n", encoding="utf-8")
        artifact = create_hole_electron_artifact(
            excited,
            stacking_protocol.hole_electron,
            metrics,
            nto=nto,
            runtime_provenance={
                "version": "synthetic-3.8",
                "executable": "/synthetic/Multiwfn",
                "menu_contract": "explicit-test-menu-v1",
            },
            files=files,
            source_structure=dimer,
        )

        self.assertIsInstance(artifact, HoleElectronArtifact)
        self.assertEqual(
            artifact.parent_artifacts,
            (excited.artifact_id, nto.artifact_id),
        )
        self.assertEqual(artifact.metadata["separation_distance_angstrom"], 2.1)
        self.assertEqual(
            artifact.provenance["runtime"]["version"], "synthetic-3.8"
        )
        self.assertEqual(
            artifact.metadata["visualization"], {"format": "cube"}
        )
        self.assertEqual(
            artifact.metadata["hole_population"],
            {"fragment_1": 0.8, "fragment_2": 0.2},
        )
        self.assertEqual(
            artifact.metadata["electron_population"],
            {"fragment_1": 0.2, "fragment_2": 0.8},
        )
        self.assertEqual(artifact.metadata["hole_extent_angstrom"], 1.2)
        restored_hole = artifact_from_dict(artifact.to_dict())
        self.assertEqual(restored_hole.artifact_id, artifact.artifact_id)
        self.assertEqual(
            restored_hole.metadata["fragment_partition_hash"],
            artifact.metadata["fragment_partition_hash"],
        )
        self.assertTrue(
            validate_hole_electron_artifact(
                artifact,
                excited_state=excited,
                protocol=stacking_protocol.hole_electron,
                source_structure=dimer,
                nto=nto,
            ).passed
        )

    def test_fragment_mapping_and_state_identity_fail_closed(self) -> None:
        dimer = self.dimer()
        excited = self.excited_artifact(dimer)
        metrics = HoleElectronMetrics(
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 2.1),
            2.1,
            0.35,
            0.65,
            {
                "fragment_1": {"hole": 0.8, "electron": 0.2},
                "fragment_2": {"hole": 0.2, "electron": 0.8},
            },
            1.2,
            1.4,
        )
        files = {
            item.role: str(self.root / f"invalid-{item.output_path}")
            for item in self.stacking_protocol().hole_electron.outputs
        }
        for path in files.values():
            selected = Path(path)
            selected.parent.mkdir(parents=True, exist_ok=True)
            selected.write_text("synthetic output\n", encoding="utf-8")
        incomplete_partition = replace(
            self.stacking_protocol().hole_electron,
            fragments=(
                FragmentDefinition("fragment_1", (0, 1, 2, 3)),
                FragmentDefinition("fragment_2", (4, 5, 6)),
            ),
        )
        with self.assertRaisesRegex(
            ValueError, "cover the source structure exactly"
        ):
            create_hole_electron_artifact(
                excited,
                incomplete_partition,
                metrics,
                runtime_provenance={
                    "version": "synthetic-3.8",
                    "executable": "/synthetic/Multiwfn",
                    "menu_contract": "explicit-test-menu-v1",
                },
                files=files,
                source_structure=dimer,
            )
        missing_state = replace(
            self.stacking_protocol().hole_electron,
            state_index=3,
        )
        with self.assertRaisesRegex(
            ValueError, "absent from the excited-state artifact"
        ):
            create_hole_electron_artifact(
                excited,
                missing_state,
                metrics,
                runtime_provenance={
                    "version": "synthetic-3.8",
                    "executable": "/synthetic/Multiwfn",
                    "menu_contract": "explicit-test-menu-v1",
                },
                files=files,
                source_structure=dimer,
            )
        with self.assertRaisesRegex(ValueError, "overlap"):
            HoleElectronProtocol(
                1,
                "explicit-test-menu-v1",
                ("18", "q"),
                self.stacking_protocol().hole_electron.outputs,
                fragments=(
                    FragmentDefinition("fragment_1", (0, 1)),
                    FragmentDefinition("fragment_2", (1, 2)),
                ),
            )

    def test_hole_electron_command_reuses_multiwfn_runtime_contract(self) -> None:
        protocol = self.stacking_protocol().hole_electron
        excited = self.excited_artifact(self.monomer)
        plan = plan_hole_electron_analysis(excited, protocol)
        executable = self.root / "Multiwfn"
        settings = self.root / "settings.ini"
        source = self.root / "excited.molden.input"
        stdin = self.root / "hole-electron.in"
        attempt = self.root / "attempt_001"
        executable.write_text("synthetic executable\n", encoding="utf-8")
        executable.chmod(0o755)
        settings.write_text("nthreads=2\n", encoding="utf-8")
        source.write_text("synthetic wavefunction\n", encoding="utf-8")
        stdin.write_text(protocol.stdin_text, encoding="utf-8")
        attempt.mkdir()
        runtime = {
            "version": "synthetic-3.8",
            "menu_contract": protocol.menu_contract,
            "executable": str(executable),
            "executable_sha256": file_hash(executable),
            "settings_path": str(settings),
            "settings_sha256": file_hash(settings),
            "requested_nthreads": 2,
        }

        command = build_hole_electron_command_spec(
            plan,
            runtime=runtime,
            source_path=source,
            attempt_directory=attempt,
            stdin_path=stdin,
        )
        self.assertEqual(command.runtime["menu_contract"], protocol.menu_contract)
        self.assertEqual(command.stdin_path, str(stdin.resolve()))
        self.assertEqual(command.outputs, protocol.outputs)

        with self.assertRaisesRegex(
            ValueError, "menu contract does not match"
        ):
            build_hole_electron_command_spec(
                plan,
                runtime={**runtime, "menu_contract": "wrong"},
                source_path=source,
                attempt_directory=attempt,
                stdin_path=stdin,
            )

        stdin.write_text("different menu\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "planned menu sequence"):
            build_hole_electron_command_spec(
                plan,
                runtime=runtime,
                source_path=source,
                attempt_directory=attempt,
                stdin_path=stdin,
            )

    def test_deferred_state_selection_cannot_render_multiwfn_command(self) -> None:
        protocol = replace(
            self.stacking_protocol().hole_electron,
            state_index=None,
            menu_contract="",
            menu_sequence=(),
            execution_ready=False,
            state_selection={
                "criteria": ("lowest_bright_state", "lowest_ct_like_state"),
                "selection_timing": "post_tddft",
            },
        )
        plan = plan_hole_electron_analysis(self.excited_artifact(), protocol)

        self.assertFalse(plan.protocol.to_dict()["execution_ready"])
        with self.assertRaises(DeferredStateSelectionError) as caught:
            build_hole_electron_command_spec(
                plan,
                runtime={},
                source_path=self.root / "unavailable.molden.input",
                attempt_directory=self.root / "attempt_001",
                stdin_path=self.root / "unavailable.in",
            )
        self.assertEqual(caught.exception.code, "DEFERRED_STATE_SELECTION")
        self.assertIn("lowest bright", str(caught.exception).replace("_", " "))


class CompleteWorkflowTests(StackingFixture):
    def test_stacking_plan_materializes_only_dependency_ready_orca_stage(self) -> None:
        dimer = assemble_vertical_dimer(
            self.monomer,
            self.template(),
            self.root / "materialized-dimer.xyz",
            charge=0,
            multiplicity=1,
        )
        protocol = replace(
            self.stacking_protocol(),
            relaxation_method=GroundStateProtocol(
                "r2SCAN-3c", "", keywords=("TightSCF",)
            ),
        )
        scientific_plan = build_vertical_stacking_workflow(
            periodic_source=self.periodic,
            monomer_a=self.monomer,
            template=self.template(),
            dimer=dimer,
            protocol=protocol,
            graph_id="materialized_vertical_stacking",
        )
        execution_plan = stacking_execution_plan(scientific_plan)
        profile = ExecutionProfile(
            "local-test",
            OrcaResourcePolicy(8, 18.0),
            MultiwfnResourcePolicy(8, 18.0),
        )
        materializer = WorkflowPlanMaterializer(
            {ORCA_RENDERER_ID: OrcaExecutionRenderer()}
        )
        materialized = materializer.materialize_ready(
            execution_plan,
            project_root=self.root / "stacking",
            system_identifier="generic_pair",
            resource_profile=profile,
            runtime_identity={"orca": {"path": "/synthetic/orca", "version": "6.1"}},
        )

        self.assertEqual(
            tuple(node.node_id for node in materialized.nodes),
            ("constrained_optimization",),
        )
        first = materialized.nodes[0]
        stage_input = Path(first.input_files["primary"]).read_text(encoding="utf-8")
        self.assertIn("! r2SCAN-3c TightSCF Opt", stage_input)
        self.assertIn("* xyzfile 0 1 input.xyz", stage_input)
        self.assertIn("{ C 0 C }", stage_input)
        self.assertIn("{ C 6 C }", stage_input)
        self.assertIn("nprocs 8", stage_input)
        self.assertEqual(
            Path(first.output_files["final_geometry"]).name, "stage.xyz"
        )
        self.assertEqual(
            first.layout.project_root, (self.root / "stacking").resolve()
        )
        self.assertNotIn(
            "calculation/calculation", str(first.layout.working_directory)
        )
        with self.assertRaises(WorkflowPlanMaterializationError) as caught:
            materializer.materialize_node(
                execution_plan,
                "full_optimization",
                project_root=self.root / "stacking",
                system_identifier="generic_pair",
                resource_profile=profile,
                runtime_identity={
                    "orca": {"path": "/synthetic/orca", "version": "6.1"}
                },
            )
        self.assertEqual(caught.exception.code, "MISSING_ARTIFACT_DEPENDENCY")

    def test_complete_dag_has_explicit_lineage_and_execution_layouts(self) -> None:
        output = self.root / "workflow-dimer.xyz"
        project_root = self.root / "calculation"
        plan = prepare_vertical_stacking_workflow(
            periodic_source=self.periodic,
            pair=self.pair,
            monomer_a=self.monomer,
            dimer_output_path=output,
            protocol=self.stacking_protocol(),
            extraction_method="ordered_core_plane_fit_v1",
            graph_id="synthetic_vertical_stacking",
            project_root=project_root,
            system_identifier="generic_pair",
        )

        expected_order = (
            "periodic_source",
            "monomer_a",
            "stacking_template",
            "vertical_dimer",
            "constrained_optimization",
            "full_optimization",
            "frequency",
            "ground_state",
            "excited_state",
            "natural_transition_orbitals",
            "hole_electron_analysis",
        )
        self.assertEqual(plan.graph.topological_order(), expected_order)
        self.assertEqual(
            plan.graph.node_map["excited_state"].dependencies,
            ("full_optimization", "ground_state"),
        )
        self.assertEqual(
            plan.graph.node_map["hole_electron_analysis"].dependencies,
            ("excited_state", "natural_transition_orbitals"),
        )
        self.assertEqual(
            {item.artifact_type for item in plan.graph.node_map[
                "hole_electron_analysis"
            ].requires},
            {"ExcitedStateArtifact", "NTOArtifact"},
        )
        self.assertEqual(plan.orca_plans["excited_state"].spec.stage_type, StageType.TDDFT)
        self.assertEqual(
            set(plan.execution_layouts),
            {
                "constrained_optimization",
                "full_optimization",
                "frequency",
                "ground_state",
                "excited_state",
                "hole_electron_analysis",
            },
        )
        first = plan.execution_layouts["excited_state"]
        self.assertEqual(first.attempt_identifier, "attempt_001")
        self.assertEqual(first.project_root, project_root.resolve())
        self.assertTrue(
            first.working_directory.is_relative_to(project_root.resolve())
        )
        self.assertEqual(
            plan.artifact_templates["full_optimization"][0].parent_artifacts,
            (
                plan.artifact_templates["constrained_optimization"][0].artifact_id,
                plan.artifact_templates["constrained_optimization"][1].artifact_id,
            ),
        )

        missing_nto = dict(plan.artifact_templates)
        del missing_nto["natural_transition_orbitals"]
        with self.assertRaisesRegex(
            StackingWorkflowValidationError, "missing parent"
        ):
            validate_stacking_artifact_lineage(plan.graph, missing_nto)


if __name__ == "__main__":
    unittest.main()
