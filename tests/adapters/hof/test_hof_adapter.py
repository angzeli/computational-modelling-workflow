from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import unittest

from cmw.adapters.hof import (
    build_hof_interaction_workflow,
    configuration_from_documents,
    load_hof_configuration,
    load_yaml_document,
    render_hof_orca_input,
)
from cmw.core.artifacts import (
    ArtifactValidation,
    CPInteractionArtifact,
    DimerEnergyArtifact,
    LEDArtifact,
    ValidationStatus,
    validate_artifact_compatibility,
)
from cmw.core.workflow_graph import (
    AggregationNode,
    CalculationNode,
    DerivedResultNode,
)
from cmw.molecular.orca.input import OrcaResources
from cmw.structure.xyz import read_xyz


FIXTURES = Path(__file__).parents[2] / "fixtures" / "hof"
CONFIG = FIXTURES / "config"
PASSED = ArtifactValidation(
    ValidationStatus.PASSED, {"synthetic": True}, "VALID", "synthetic fixture"
)


class HofAdapterTests(unittest.TestCase):
    def documents(self):
        return (
            load_yaml_document(CONFIG / "systems.yaml"),
            load_yaml_document(CONFIG / "methods.yaml"),
            load_yaml_document(CONFIG / "protocol.yaml"),
        )

    def configuration(self, systems=None, methods=None, protocol=None):
        loaded = self.documents()
        return configuration_from_documents(
            systems=systems or loaded[0],
            methods=methods or loaded[1],
            protocol=protocol or loaded[2],
            system_id="synthetic_hof",
            project_root=FIXTURES,
            geometry=read_xyz(FIXTURES / "structure/input_structure/dimer.xyz"),
        )

    def test_valid_hof_dimer_maps_to_generic_dag_and_typed_artifacts(self) -> None:
        configuration = load_hof_configuration(
            systems_path=CONFIG / "systems.yaml",
            methods_path=CONFIG / "methods.yaml",
            protocol_path=CONFIG / "protocol.yaml",
            system_id="synthetic_hof",
        )
        plan = build_hof_interaction_workflow(configuration)

        self.assertEqual(
            plan.graph.topological_order(),
            ("dimer", "fragment_left", "fragment_right", "cp_interaction", "led"),
        )
        self.assertIsInstance(plan.graph.node_map["dimer"], CalculationNode)
        self.assertIsInstance(plan.graph.node_map["cp_interaction"], AggregationNode)
        self.assertIsInstance(plan.graph.node_map["led"], DerivedResultNode)
        self.assertEqual(
            set(plan.graph.ready_nodes({"dimer"})),
            {"fragment_left", "fragment_right"},
        )
        self.assertIsInstance(plan.artifact_templates["dimer"][0], DimerEnergyArtifact)
        self.assertIsInstance(
            plan.artifact_templates["cp_interaction"][0], CPInteractionArtifact
        )
        self.assertIsInstance(plan.artifact_templates["led"][0], LEDArtifact)
        self.assertEqual(configuration.system.atom_to_fragment[4], "right")

        validated = {
            node_id: tuple(
                replace(artifact, validation=PASSED) for artifact in artifacts
            )
            for node_id, artifacts in plan.artifact_templates.items()
        }
        cp_parents = plan.graph.validate_artifacts("cp_interaction", validated)
        validate_artifact_compatibility(
            plan.artifact_templates["cp_interaction"][0], cp_parents
        )
        led_parents = plan.graph.validate_artifacts("led", validated)
        validate_artifact_compatibility(plan.artifact_templates["led"][0], led_parents)

    def test_fragment_aware_orca_inputs_cover_led_and_counterpoise(self) -> None:
        configuration = self.configuration()
        plan = build_hof_interaction_workflow(configuration)
        resources = OrcaResources(4, 2000)
        dimer = render_hof_orca_input(
            system=configuration.system,
            calculation=plan.orca_calculations["dimer"],
            resources=resources,
        )
        fragment = render_hof_orca_input(
            system=configuration.system,
            calculation=plan.orca_calculations["fragment_left"],
            resources=resources,
        )

        self.assertIn("TightPNO TightSCF LED SP", dimer.splitlines()[0])
        self.assertIn("O(1)", dimer)
        self.assertIn("O(2)", dimer)
        self.assertNotIn(" LED ", fragment.splitlines()[0])
        self.assertEqual(sum(":" in line for line in fragment.splitlines()[4:-1]), 3)
        self.assertIn("O:", fragment)
        self.assertTrue(
            plan.orca_calculations["fragment_left"].spec.protocol["counterpoise"]
        )

    def test_optional_execution_config_is_loaded_without_changing_science(self) -> None:
        base = load_hof_configuration(
            systems_path=CONFIG / "systems.yaml",
            methods_path=CONFIG / "methods.yaml",
            protocol_path=CONFIG / "protocol.yaml",
            system_id="synthetic_hof",
        )
        configured = load_hof_configuration(
            systems_path=CONFIG / "systems.yaml",
            methods_path=CONFIG / "methods.yaml",
            protocol_path=CONFIG / "protocol.yaml",
            execution_path=CONFIG / "execution.yaml",
            system_id="synthetic_hof",
        )
        base_plan = build_hof_interaction_workflow(base)
        configured_plan = build_hof_interaction_workflow(configured)

        self.assertNotIn("execution_profile", base.to_dict())
        self.assertNotIn("resources", base_plan.orca_calculations["dimer"].to_dict())
        self.assertEqual(configured.execution_profile.name, "local_mac")
        self.assertEqual(configured.execution_profile.orca.total_memory_gb, 18.0)
        self.assertEqual(
            {key: value.calculation_id for key, value in base_plan.orca_calculations.items()},
            {
                key: value.calculation_id
                for key, value in configured_plan.orca_calculations.items()
            },
        )
        self.assertEqual(
            {
                artifact.artifact_id
                for values in base_plan.artifact_templates.values()
                for artifact in values
            },
            {
                artifact.artifact_id
                for values in configured_plan.artifact_templates.values()
                for artifact in values
            },
        )
        dimer = configured_plan.orca_calculations["dimer"]
        self.assertEqual(dimer.resources, OrcaResources(8, 1843))
        self.assertEqual(dimer.execution["derived_total_memory_mb"], 14_744)

        rendered = render_hof_orca_input(
            system=configured.system,
            calculation=dimer,
        )
        self.assertIn("%pal nprocs 8 end", rendered)
        self.assertIn("%maxcore 1843", rendered)

    def test_invalid_fragment_partition_is_rejected(self) -> None:
        systems, methods, protocol = self.documents()
        overlapping = deepcopy(systems)
        overlapping["systems"]["synthetic_hof"]["fragments"]["right"][
            "atom_indices"
        ] = [
            2,
            3,
            4,
            5,
        ]
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.configuration(overlapping, methods, protocol)

        incomplete = deepcopy(systems)
        incomplete["systems"]["synthetic_hof"]["fragments"]["right"]["atom_indices"] = [
            3,
            4,
        ]
        with self.assertRaisesRegex(ValueError, "do not cover every atom"):
            self.configuration(incomplete, methods, protocol)

    def test_invalid_hydrogen_bond_definition_is_rejected(self) -> None:
        systems, methods, protocol = self.documents()
        invalid = deepcopy(systems)
        invalid["systems"]["synthetic_hof"]["hydrogen_bonds"]["bonds"][0][
            "acceptor_atom"
        ] = 2
        with self.assertRaisesRegex(ValueError, "opposite fragment"):
            self.configuration(invalid, methods, protocol)

        invalid_hydrogen = deepcopy(systems)
        invalid_hydrogen["systems"]["synthetic_hof"]["hydrogen_bonds"]["bonds"][0][
            "hydrogen_atom"
        ] = 2
        with self.assertRaisesRegex(ValueError, "is not hydrogen"):
            self.configuration(invalid_hydrogen, methods, protocol)

        split_donor = deepcopy(systems)
        split_donor["systems"]["synthetic_hof"]["hydrogen_bonds"]["bonds"][0][
            "hydrogen_atom"
        ] = 4
        with self.assertRaisesRegex(ValueError, "different fragments"):
            self.configuration(split_donor, methods, protocol)

        wrong_count = deepcopy(systems)
        wrong_count["systems"]["synthetic_hof"]["hydrogen_bonds"]["expected_count"] = 3
        with self.assertRaisesRegex(ValueError, "count does not match"):
            self.configuration(wrong_count, methods, protocol)

    def test_one_based_source_indices_normalize_to_internal_zero_based(self) -> None:
        systems, methods, protocol = self.documents()
        one_based = deepcopy(systems)
        selected = one_based["systems"]["synthetic_hof"]
        selected["atom_index_base"] = 1
        for fragment in selected["fragments"].values():
            fragment["atom_indices"] = [index + 1 for index in fragment["atom_indices"]]
        for bond in selected["hydrogen_bonds"]["bonds"]:
            for key in ("donor_atom", "hydrogen_atom", "acceptor_atom"):
                bond[key] += 1

        configuration = self.configuration(one_based, methods, protocol)
        self.assertEqual(configuration.system.fragments[0].atom_indices, (0, 1, 2))
        self.assertEqual(
            configuration.system.to_dict()["fragments"][0]["atom_indices"],
            [1, 2, 3],
        )

    def test_missing_fragment_is_rejected(self) -> None:
        systems, methods, protocol = self.documents()
        invalid = deepcopy(systems)
        invalid["systems"]["synthetic_hof"]["fragments"].pop("right")
        with self.assertRaisesRegex(ValueError, "exactly two fragments"):
            self.configuration(invalid, methods, protocol)

    def test_incompatible_protocol_is_rejected(self) -> None:
        systems, methods, protocol = self.documents()
        invalid = deepcopy(methods)
        invalid["high_level"]["method"] = "B3LYP"
        with self.assertRaisesRegex(ValueError, "requires a DLPNO method"):
            self.configuration(systems, invalid, protocol)

        missing_basis = deepcopy(methods)
        missing_basis["high_level"]["basis"] = None
        with self.assertRaisesRegex(ValueError, "basis must be a non-empty string"):
            self.configuration(systems, missing_basis, protocol)

    def test_symmetry_equivalent_metadata_must_be_consistent(self) -> None:
        systems, methods, protocol = self.documents()
        invalid = deepcopy(systems)
        invalid["systems"]["synthetic_hof"]["hydrogen_bonds"]["bonds"][0][
            "symmetry_group"
        ] = "pair-1"
        with self.assertRaisesRegex(ValueError, "symmetry metadata"):
            self.configuration(invalid, methods, protocol)


if __name__ == "__main__":
    unittest.main()
