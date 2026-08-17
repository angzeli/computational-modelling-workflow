from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from cmw.adapters.hof import (
    build_hof_workflow_plan,
    configuration_from_documents,
    load_hof_configuration,
    load_yaml_document,
    render_hof_orca_input,
)
from cmw.core.artifacts import (
    DeformationEnergyArtifact,
    DensityArtifact,
    FrequencyArtifact,
    IGMHArtifact,
    OptimizationArtifact,
    StructureArtifact,
    artifact_from_dict,
)
from cmw.core.workflow_graph import AggregationNode
from cmw.molecular.orca.input import OrcaResources
from cmw.molecular.orca.status import StageType
from cmw.structure.xyz import read_xyz


FIXTURES = Path(__file__).parents[2] / "fixtures" / "hof"
CONFIG = FIXTURES / "config"


class HofWorkflowPlannerTests(unittest.TestCase):
    def documents(self):
        return (
            load_yaml_document(CONFIG / "systems.yaml"),
            load_yaml_document(CONFIG / "methods.yaml"),
            load_yaml_document(CONFIG / "protocol.yaml"),
        )

    def configuration(self, *, methods=None, protocol=None):
        systems, fixture_methods, fixture_protocol = self.documents()
        return configuration_from_documents(
            systems=systems,
            methods=methods or fixture_methods,
            protocol=protocol or fixture_protocol,
            system_id="synthetic_hof",
            project_root=FIXTURES,
            geometry=read_xyz(FIXTURES / "structure/input_structure/dimer.xyz"),
        )

    def plan(self, *, execution: bool = False):
        configuration = load_hof_configuration(
            systems_path=CONFIG / "systems.yaml",
            methods_path=CONFIG / "methods.yaml",
            protocol_path=CONFIG / "protocol.yaml",
            execution_path=(CONFIG / "execution.yaml") if execution else None,
            system_id="synthetic_hof",
        )
        return build_hof_workflow_plan(configuration)

    def test_complete_hof_workflow_graph_is_composed_and_closed(self) -> None:
        plan = self.plan()

        self.assertEqual(len(plan.graph.nodes), 18)
        self.assertEqual(plan.graph.external_inputs, ())
        self.assertEqual(
            set(plan.metadata["branches"]),
            {"geometry", "interaction", "deformation", "density", "igmh"},
        )
        self.assertEqual(plan.metadata["execution"], "planned_only")
        self.assertFalse(plan.metadata["synthetic_results"])
        self.assertEqual(set(plan.artifact_templates), set(plan.graph.node_map))
        self.assertEqual(len(plan.orca_calculations), 12)
        json.dumps(plan.to_dict())

    def test_geometry_branch_maps_structure_to_optimization_plan(self) -> None:
        plan = self.plan()
        geometry = plan.graph.node_map["geometry_optimization"]
        artifacts = plan.artifact_templates["geometry_optimization"]

        self.assertEqual(geometry.dependencies, ("input_structure",))
        self.assertEqual(
            set(geometry.produces), {"OptimizationArtifact", "StructureArtifact"}
        )
        self.assertIsInstance(artifacts[0], OptimizationArtifact)
        self.assertIsInstance(artifacts[1], StructureArtifact)
        self.assertEqual(artifacts[1].source, "geometry_optimization")
        self.assertEqual(artifacts[1].geometry_hash, "")
        self.assertEqual(
            plan.orca_calculations["geometry_optimization"].spec.stage_type,
            StageType.OPT,
        )
        self.assertTrue(
            plan.orca_calculations["geometry_optimization"].spec.protocol[
                "frequency_requested"
            ]
        )
        frequency = plan.graph.node_map["geometry_frequency"]
        promoted = plan.graph.node_map["validated_geometry"]
        self.assertEqual(frequency.dependencies, ("geometry_optimization",))
        self.assertEqual(frequency.produces, ("FrequencyArtifact",))
        self.assertIsInstance(
            plan.artifact_templates["geometry_frequency"][0], FrequencyArtifact
        )
        self.assertEqual(
            plan.orca_calculations["geometry_frequency"].spec.stage_type,
            StageType.FREQ,
        )
        self.assertEqual(
            promoted.dependencies,
            ("geometry_optimization", "geometry_frequency"),
        )
        self.assertEqual(promoted.produces, ("StructureArtifact",))
        self.assertEqual(
            plan.artifact_templates["validated_geometry"][0].source,
            "validated_opt_freq",
        )
        with tempfile.TemporaryDirectory() as temporary:
            geometry_path = Path(temporary) / "input.xyz"
            rendered = render_hof_orca_input(
                system=plan.configuration.system,
                calculation=plan.orca_calculations["geometry_optimization"],
                resources=OrcaResources(),
                geometry_path=geometry_path,
            )
            self.assertTrue(geometry_path.is_file())
        self.assertIn("* xyzfile 0 1 input.xyz", rendered)
        self.assertEqual(rendered.splitlines()[0].split()[-1], "Opt")
        self.assertEqual(
            geometry.configuration["execution_intent"],
            {
                "stage_type": "OPT",
                "task": "optimization",
                "required_behavior": "Opt",
            },
        )
        geometry_plan = plan.orca_calculations["geometry_optimization"].to_dict()[
            "geometry_input"
        ]
        self.assertEqual(geometry_plan["mode"], "xyzfile")
        self.assertEqual(geometry_plan["input_geometry_file"], "input.xyz")
        self.assertEqual(geometry_plan["geometry_contract_file"], "geometry-input.json")
        self.assertEqual(
            geometry_plan["geometry_hash"],
            geometry_plan["source_structure_artifact"]["geometry_hash"],
        )
        self.assertEqual(
            geometry_plan["source_structure_artifact"]["artifact_type"],
            "StructureArtifact",
        )

    def test_interaction_branch_uses_composed_optimized_structure(self) -> None:
        plan = self.plan()
        dimer = plan.graph.node_map["dimer"]
        optimized = plan.artifact_templates["validated_geometry"][0]
        dimer_energy, wavefunction = plan.artifact_templates["dimer"]

        self.assertEqual(dimer.dependencies, ("validated_geometry",))
        self.assertIn(optimized.artifact_id, dimer_energy.parent_artifacts)
        self.assertIn(optimized.artifact_id, wavefunction.parent_artifacts)
        self.assertEqual(
            plan.orca_calculations["dimer"].spec.protocol["method"],
            "DLPNO-CCSD(T)",
        )
        self.assertEqual(
            plan.orca_calculations["dimer"].spec.protocol["auxiliary_basis"],
            {
                "correlation": "def2-TZVPP/C",
                "coulomb_exchange": "def2/JK",
            },
        )
        self.assertIn(
            "def2-TZVPP/C def2/JK RIJK",
            plan.orca_calculations["dimer"].spec.keywords,
        )
        self.assertEqual(
            set(plan.graph.node_map["cp_interaction"].dependencies),
            {"dimer", "fragment_left", "fragment_right"},
        )

    def test_deformation_branch_plans_distorted_and_relaxed_fragment_energies(self) -> None:
        plan = self.plan()
        node = plan.graph.node_map["deformation_energy"]
        artifact = plan.artifact_templates["deformation_energy"][0]

        self.assertIsInstance(node, AggregationNode)
        self.assertEqual(len(node.dependencies), 4)
        self.assertTrue(
            all(
                dependency.startswith(("distorted_fragment_", "relaxed_fragment_"))
                for dependency in node.dependencies
            )
        )
        self.assertIsInstance(artifact, DeformationEnergyArtifact)
        self.assertEqual(len(artifact.parent_artifacts), 4)
        self.assertNotIn("energy", artifact.metadata)
        self.assertEqual(
            artifact_from_dict(artifact.to_dict()).artifact_id, artifact.artifact_id
        )
        self.assertEqual(
            plan.orca_calculations["relax_fragment_left"].spec.stage_type,
            StageType.OPT,
        )
        self.assertEqual(
            plan.orca_calculations["relaxed_fragment_left_energy"].spec.stage_type,
            StageType.SP,
        )
        parents = {
            parent.artifact_id: parent
            for node_artifacts in plan.artifact_templates.values()
            for parent in node_artifacts
            if parent.artifact_id in artifact.parent_artifacts
        }
        self.assertEqual(
            {parent.metadata["geometry_state"] for parent in parents.values()},
            {"distorted", "relaxed"},
        )

    def test_density_and_generic_igmh_branches_are_composed(self) -> None:
        plan = self.plan()
        density = plan.artifact_templates["igmh_density"][0]
        igmh = plan.artifact_templates["multiwfn_igmh"][0]
        multiwfn_plan = plan.multiwfn_plans["multiwfn_igmh"]

        self.assertEqual(
            plan.graph.node_map["igmh_density"].dependencies,
            ("validated_geometry",),
        )
        self.assertEqual(
            plan.graph.node_map["multiwfn_igmh"].dependencies, ("igmh_density",)
        )
        self.assertIsInstance(density, DensityArtifact)
        self.assertIsInstance(igmh, IGMHArtifact)
        self.assertEqual(igmh.parent_artifacts, (density.artifact_id,))
        self.assertFalse(multiwfn_plan["execute"])
        self.assertTrue(multiwfn_plan["grid_spacing_required_at_execution"])
        self.assertEqual(
            {item["role"] for item in multiwfn_plan["outputs"]},
            {"delta_g_inter_cube", "sign_lambda2_rho_cube"},
        )
        self.assertIsNone(igmh.provenance["multiwfn_version"])
        self.assertEqual(igmh.metadata["density_source"], density.artifact_id)
        self.assertIsNone(igmh.metadata["grid_spacing_bohr"])
        self.assertEqual(igmh.metadata["multiwfn_protocol"], multiwfn_plan)

    def test_artifact_lineage_is_complete_and_has_only_declared_terminals(self) -> None:
        plan = self.plan()
        artifacts = [
            artifact
            for node_artifacts in plan.artifact_templates.values()
            for artifact in node_artifacts
        ]
        known = {artifact.artifact_id for artifact in artifacts}
        referenced = {
            parent
            for artifact in artifacts
            for parent in artifact.parent_artifacts
        }

        self.assertTrue(referenced.issubset(known))
        terminals = {
            artifact.artifact_type
            for artifact in artifacts
            if artifact.artifact_id not in referenced
        }
        self.assertEqual(
            terminals,
            {"LEDArtifact", "DeformationEnergyArtifact", "IGMHArtifact"},
        )

    def test_execution_profile_populates_plans_not_scientific_identities(self) -> None:
        base = self.plan()
        configured = self.plan(execution=True)

        self.assertEqual(
            {
                node_id: plan.calculation_id
                for node_id, plan in base.orca_calculations.items()
            },
            {
                node_id: plan.calculation_id
                for node_id, plan in configured.orca_calculations.items()
            },
        )
        self.assertEqual(
            {
                artifact.artifact_id
                for artifacts in base.artifact_templates.values()
                for artifact in artifacts
            },
            {
                artifact.artifact_id
                for artifacts in configured.artifact_templates.values()
                for artifact in artifacts
            },
        )
        for calculation in configured.orca_calculations.values():
            self.assertEqual(calculation.resources.nprocs, 8)
            self.assertEqual(calculation.resources.maxcore_mb_per_process, 1843)
            self.assertEqual(calculation.execution["total_memory_gb"], 18.0)
        runtime = configured.multiwfn_plans["multiwfn_igmh"]["runtime"]
        self.assertEqual(runtime["nthreads"], 8)
        self.assertEqual(runtime["total_memory_gb"], 18.0)
        self.assertNotIn("maxcore", runtime)
        self.assertEqual(
            configured.metadata["execution_profile"]["name"], "local_mac"
        )

    def test_missing_branch_methods_fail_during_configuration_resolution(self) -> None:
        systems, methods, protocol = self.documents()
        del systems
        missing_geometry = deepcopy(methods)
        missing_geometry.pop("geometry")
        with self.assertRaisesRegex(ValueError, "method_ref is not defined: geometry"):
            self.configuration(methods=missing_geometry, protocol=protocol)

        missing_density = deepcopy(methods)
        missing_density.pop("igmh_density")
        with self.assertRaisesRegex(
            ValueError, "method_ref is not defined: igmh_density"
        ):
            self.configuration(methods=missing_density, protocol=protocol)


if __name__ == "__main__":
    unittest.main()
