from __future__ import annotations

import unittest

from cmw.core.artifacts import (
    ArtifactValidation,
    SinglePointArtifact,
    ValidationStatus,
)
from cmw.core.workflow_graph import WorkflowGraph
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


if __name__ == "__main__":
    unittest.main()
