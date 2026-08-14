# Composite scientific workflow engine

CMW separates three concerns that were previously implicit in a linear
OPT/FREQ/SP workflow:

1. a system-agnostic directed acyclic graph describes dependency and fan-in;
2. typed scientific artifacts describe what each node produced; and
3. adapter-specific protocol validation proves that execution output matches
   declared scientific intent.

The graph engine does not contain molecule names, fragment names, hydrogen-bond
logic, or counterpoise equations. Scientific operations build on the generic
node and artifact contracts.

## Workflow graph

`cmw.core.workflow_graph.WorkflowGraph` supports explicit `CalculationNode`,
`AggregationNode`, and `DerivedResultNode` types. Each node can declare
dependencies, required artifact types, and produced artifact types. The legacy
`WorkflowNode` constructor remains available as a compatibility surface, while
schema-loaded nodes use the explicit types.

```yaml
workflow:
  example_interaction:
    nodes:
      - id: supersystem
        method: high_level
        produces: [DimerEnergyArtifact]

      - id: subsystem_1
        method: high_level
        role: fragment_calculation
        produces: [FragmentEnergyArtifact]

      - id: subsystem_2
        method: high_level
        role: fragment_calculation
        produces: [FragmentEnergyArtifact]

    aggregation:
      id: interaction
      type: counterpoise_interaction
      depends_on: [supersystem, subsystem_1, subsystem_2]
      requires:
        - artifact_type: EnergyArtifact
          count: 3
      produces: [CPInteractionArtifact]
```

Names such as `supersystem` and `subsystem_1` are project choices. The graph
validates only generic structure and contracts:

- node identifiers are unique;
- dependencies exist;
- dependency cycles are rejected;
- aggregation and derived nodes have dependencies;
- declared requirements can be supplied by dependency outputs; and
- actual dependency artifacts exist, are valid, and satisfy required types.

The original OPT/FREQ/SP modes are represented internally by
`legacy_workflow_graph()`. Their public CLI, stage order, dependencies, and
result values remain unchanged.

## Typed artifacts

`cmw.core.artifacts` provides a small hierarchy:

```text
Artifact
├── CalculationArtifact
│   ├── EnergyArtifact
│   │   ├── SinglePointArtifact
│   │   ├── DimerEnergyArtifact
│   │   ├── FragmentEnergyArtifact
│   │   └── InteractionEnergyArtifact
│   │       └── CPInteractionArtifact
│   ├── OptimizationArtifact
│   ├── FrequencyArtifact
│   └── WavefunctionArtifact
└── AnalysisArtifact
    ├── LEDArtifact
    ├── DensityArtifact
    └── IGMHArtifact
```

Every artifact records its producing calculation, method, basis, protocol,
parent artifact identities, file locations, validation decision, provenance,
and type-specific metadata. Its stable identity is based on scientific content,
not mutable file locations or a later validation verdict.

Compatibility is checked separately from graph traversal. Current rules cover:

- comparable dimer/supersystem and fragment energies for a CP artifact;
- a valid DLPNO calculation with LED enabled for an LED artifact; and
- a valid density parent for an IGMH artifact.

The rules can be extended without teaching the graph engine about a particular
scientific operation. Existing ORCA and Multiwfn results retain their original
artifact manifests and additionally expose `scientific_artifact`. Result readers
continue to accept older records that predate this field.

## Method-aware ORCA validation

An ORCA stage can add explicit protocol intent alongside its existing keywords
and blocks:

```json
{
  "keywords": "DLPNO-CCSD(T) def2-TZVPP TightPNO",
  "blocks": [],
  "protocol": {
    "method": "DLPNO-CCSD(T)",
    "basis": "def2-TZVPP",
    "pno": "TightPNO",
    "led": true,
    "fragments_required": true,
    "expected_fragments": 2
  }
}
```

For compact configs, the same protocol keys may be placed directly in the stage
object. Conflicting direct and nested values are rejected.

ORCA output parsing records explicit method, basis, PNO, LED, fragment, SCF,
optimization, and frequency evidence. Finalization compares that evidence with
the target protocol. Any required check that is absent or contradictory produces
`FAILED_PROTOCOL_MISMATCH`, marks the scientific result invalid, records all
checks in the typed artifact, and prevents reuse.

Legacy stage configurations without a protocol object continue to use the
existing OPT/FREQ/SP scientific validation policy. Method-aware validation adds
no inferred method requirement to those existing targets.
