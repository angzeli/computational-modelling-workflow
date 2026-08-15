from __future__ import annotations

import unittest

from cmw.core.artifacts import (
    ArtifactValidation,
    SinglePointArtifact,
    ValidationStatus,
)
from cmw.core.workflow_graph import (
    AggregationNode,
    ArtifactBinding,
    ArtifactRequirement,
    CalculationNode,
    DerivedResultNode,
    WorkflowCompositionError,
    WorkflowGraph,
    compose_workflow_graphs,
)
from cmw.molecular.workflows.opt_freq_sp import legacy_workflow_graph


def _energy(calculation: str) -> SinglePointArtifact:
    return SinglePointArtifact(
        producing_calculation=calculation,
        method="synthetic",
        basis="synthetic-basis",
        validation=ArtifactValidation(
            ValidationStatus.PASSED, {"synthetic": True}, "VALID", "synthetic fixture"
        ),
    )


class WorkflowGraphTests(unittest.TestCase):
    def test_existing_linear_workflow_dependencies_are_preserved(self) -> None:
        graph = legacy_workflow_graph("opt+freq+sp")
        self.assertEqual(graph.topological_order(), ("OPT", "FREQ", "SP"))
        self.assertEqual(graph.node_map["OPT"].dependencies, ())
        self.assertEqual(graph.node_map["FREQ"].dependencies, ("OPT",))
        self.assertEqual(graph.node_map["SP"].dependencies, ("OPT", "FREQ"))
        self.assertEqual(legacy_workflow_graph("opt+sp").topological_order(), ("OPT", "SP"))

    def test_branched_toy_workflow_fans_in_to_generic_aggregation(self) -> None:
        graph = WorkflowGraph.from_mapping(
            {
                "workflow": {
                    "toy": {
                        "nodes": [
                            {
                                "id": "parent",
                                "method": "method-a",
                                "produces": ["SinglePointArtifact"],
                            },
                            {
                                "id": "child_left",
                                "depends_on": ["parent"],
                                "produces": ["SinglePointArtifact"],
                            },
                            {
                                "id": "child_right",
                                "depends_on": ["parent"],
                                "produces": ["SinglePointArtifact"],
                            },
                        ],
                        "aggregation": {
                            "id": "combine",
                            "type": "synthetic_sum",
                            "depends_on": ["child_left", "child_right"],
                            "requires": [
                                {"artifact_type": "EnergyArtifact", "count": 2}
                            ],
                            "produces": ["InteractionEnergyArtifact"],
                        },
                    }
                }
            }
        )
        self.assertEqual(
            graph.topological_order(), ("parent", "child_left", "child_right", "combine")
        )
        self.assertIsInstance(graph.node_map["parent"], CalculationNode)
        self.assertIsInstance(graph.node_map["combine"], AggregationNode)
        self.assertEqual(set(graph.ready_nodes({"parent"})), {"child_left", "child_right"})
        parents = graph.validate_artifacts(
            "combine", {"child_left": _energy("left"), "child_right": _energy("right")}
        )
        self.assertEqual(len(parents), 2)

    def test_aggregation_shorthand_depends_on_all_declared_nodes(self) -> None:
        graph = WorkflowGraph.from_mapping(
            {
                "workflow": {
                    "example": {
                        "nodes": [{"id": "a"}, {"id": "b"}],
                        "aggregation": {"type": "generic_aggregation"},
                    }
                }
            }
        )
        self.assertEqual(graph.node_map["aggregation"].dependencies, ("a", "b"))

    def test_explicit_node_classes_preserve_kind_and_mapping_contracts(self) -> None:
        calculation = CalculationNode("calculate")
        aggregation = AggregationNode("combine", dependencies=("calculate",))
        derived = DerivedResultNode("derive", dependencies=("combine",))
        graph = WorkflowGraph("explicit", (calculation, aggregation, derived))

        self.assertEqual(graph.topological_order(), ("calculate", "combine", "derive"))
        self.assertEqual(aggregation.to_dict()["kind"], "aggregation")
        self.assertIsInstance(
            DerivedResultNode.from_mapping(
                {"id": "mapped", "depends_on": ["combine"]}
            ),
            DerivedResultNode,
        )
        with self.assertRaisesRegex(ValueError, "cannot be created"):
            AggregationNode.from_mapping({"id": "bad", "kind": "calculation"})

    def test_missing_and_circular_dependencies_fail_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing dependencies"):
            WorkflowGraph.from_mapping(
                {"nodes": [{"id": "only", "depends_on": ["absent"]}]}
            )
        with self.assertRaisesRegex(ValueError, "dependency cycle"):
            WorkflowGraph.from_mapping(
                {
                    "nodes": [
                        {"id": "first", "depends_on": ["second"]},
                        {"id": "second", "depends_on": ["first"]},
                    ]
                }
            )

    def test_declared_and_runtime_artifact_failures_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "dependencies declare 1"):
            WorkflowGraph.from_mapping(
                {
                    "nodes": [
                        {"id": "source", "produces": ["SinglePointArtifact"]},
                        {
                            "id": "derived",
                            "kind": "derived",
                            "depends_on": ["source"],
                            "requires": [
                                {"artifact_type": "SinglePointArtifact", "count": 2}
                            ],
                        },
                    ]
                }
            )
        graph = WorkflowGraph.from_mapping(
            {
                "nodes": [
                    {"id": "source", "produces": ["SinglePointArtifact"]},
                    {
                        "id": "derived",
                        "kind": "derived",
                        "depends_on": ["source"],
                        "requires": ["SinglePointArtifact"],
                    },
                ]
            }
        )
        with self.assertRaisesRegex(ValueError, "lacks artifacts"):
            graph.validate_artifacts("derived", {})
        invalid = SinglePointArtifact(
            producing_calculation="bad",
            validation=ArtifactValidation(ValidationStatus.FAILED, {}, "BAD", "bad"),
        )
        with self.assertRaisesRegex(ValueError, "received invalid artifact"):
            graph.validate_artifacts("derived", {"source": invalid})


class WorkflowCompositionTests(unittest.TestCase):
    def test_two_graphs_compose_through_a_typed_artifact_binding(self) -> None:
        geometry = WorkflowGraph(
            "geometry",
            (
                CalculationNode("structure", produces=("StructureArtifact",)),
                CalculationNode(
                    "optimization",
                    dependencies=("structure",),
                    requires=(
                        ArtifactRequirement(
                            "StructureArtifact", from_nodes=("structure",)
                        ),
                    ),
                    produces=("OptimizationArtifact", "StructureArtifact"),
                ),
            ),
            provenance={"builder": "geometry"},
        )
        analysis = WorkflowGraph(
            "analysis",
            (
                CalculationNode(
                    "density",
                    requires=(ArtifactRequirement("StructureArtifact"),),
                    produces=("DensityArtifact",),
                ),
            ),
            external_inputs=("StructureArtifact",),
            provenance={"builder": "analysis"},
        )

        composed = WorkflowGraph.compose(
            "composed",
            (geometry, analysis),
            bindings=(
                ArtifactBinding(
                    "optimization", "density", "StructureArtifact"
                ),
            ),
            provenance={"request": "synthetic-test"},
        )

        self.assertEqual(
            composed.topological_order(),
            ("structure", "optimization", "density"),
        )
        self.assertEqual(composed.node_map["density"].dependencies, ("optimization",))
        self.assertEqual(composed.external_inputs, ())
        self.assertEqual(composed.provenance["request"], "synthetic-test")
        self.assertEqual(
            [
                item["graph_id"]
                for item in composed.provenance["composition"]["source_graphs"]
            ],
            ["geometry", "analysis"],
        )

    def test_composition_preserves_existing_dependencies(self) -> None:
        upstream = WorkflowGraph(
            "upstream",
            (CalculationNode("source", produces=("StructureArtifact",)),),
        )
        downstream = WorkflowGraph(
            "downstream",
            (
                CalculationNode("prepare"),
                CalculationNode(
                    "consume",
                    dependencies=("prepare",),
                    requires=(ArtifactRequirement("StructureArtifact"),),
                ),
            ),
            external_inputs=("StructureArtifact",),
        )

        composed = compose_workflow_graphs(
            "composed",
            (upstream, downstream),
            bindings=(ArtifactBinding("source", "consume", "StructureArtifact"),),
        )

        self.assertEqual(
            composed.node_map["consume"].dependencies, ("prepare", "source")
        )
        self.assertLess(
            composed.topological_order().index("prepare"),
            composed.topological_order().index("consume"),
        )

    def test_identical_duplicate_nodes_are_deduplicated(self) -> None:
        shared = CalculationNode("shared", produces=("StructureArtifact",))
        left = WorkflowGraph("left", (shared,))
        right = WorkflowGraph(
            "right",
            (
                shared,
                CalculationNode("right", dependencies=("shared",)),
            ),
        )

        composed = compose_workflow_graphs("composed", (left, right))

        self.assertEqual(tuple(composed.node_map), ("shared", "right"))

    def test_node_identity_and_artifact_contract_conflicts_are_rejected(self) -> None:
        left = WorkflowGraph(
            "left", (CalculationNode("shared", operation="first"),)
        )
        conflicting = WorkflowGraph(
            "conflicting", (CalculationNode("shared", operation="second"),)
        )
        with self.assertRaisesRegex(WorkflowCompositionError, "identity conflict"):
            compose_workflow_graphs("bad", (left, conflicting))

        producer = WorkflowGraph(
            "producer",
            (CalculationNode("energy", produces=("SinglePointArtifact",)),),
        )
        consumer = WorkflowGraph(
            "consumer",
            (
                CalculationNode(
                    "structure_consumer",
                    requires=(ArtifactRequirement("StructureArtifact"),),
                ),
            ),
            external_inputs=("StructureArtifact",),
        )
        with self.assertRaisesRegex(WorkflowCompositionError, "does not produce"):
            compose_workflow_graphs(
                "bad-contract",
                (producer, consumer),
                bindings=(
                    ArtifactBinding(
                        "energy", "structure_consumer", "StructureArtifact"
                    ),
                ),
            )


if __name__ == "__main__":
    unittest.main()
