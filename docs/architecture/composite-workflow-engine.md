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

### Graph composition

`WorkflowGraph.compose()` and `compose_workflow_graphs()` merge independently
built graphs without renaming their nodes. An `ArtifactBinding` connects a
producing node to a downstream typed requirement; the composition operation
adds only that dependency and then validates the complete DAG.

```python
composed = WorkflowGraph.compose(
    "geometry_plus_analysis",
    (geometry_graph, analysis_graph),
    bindings=(
        ArtifactBinding("optimization", "density", "StructureArtifact"),
    ),
)
```

Node identity is a contract: identical duplicate nodes are deduplicated, while
different definitions with the same identifier are rejected. Bindings fail if
the source does not declare the bound artifact type or the target has no
compatible requirement. Existing dependency order, external inputs, and each
source graph's provenance are retained in the composed graph manifest; an
external input is removed only when composed producers fully satisfy it.

## Typed artifacts

`cmw.core.artifacts` provides a small hierarchy:

```text
Artifact
├── StructureArtifact
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

### Structure artifacts

`StructureArtifact` gives input, optimized, intermediate, and generated
geometries first-class identity. Its structure-specific manifest fields are the
source, format, atom count, elemental composition, charge, multiplicity, and a
geometry hash; file locations, general provenance, validation, and lineage use
the same fields as every other artifact. The geometry hash participates in the
stable artifact identity, while its storage path and later validation verdict
do not.

`structure_artifact_from_file()` constructs a validated artifact using the
registered reader for the declared format or file suffix. The initial registry
contains the existing strict XYZ reader, but `StructureFormatHandler` provides
the extension point for future structure formats. Generic validation fails
closed for missing files, unknown formats, unreadable geometry, missing
metadata, and metadata that no longer matches the file. It does not evaluate
bonding, fragments, or any project-specific chemistry.

Any validated `StructureArtifact` can be declared as a parent of an
`OptimizationArtifact`, `FrequencyArtifact`, `SinglePointArtifact`, or
`DensityArtifact`. An optimization node may also emit a new
`StructureArtifact`, allowing downstream branches to bind explicitly to the
optimized geometry rather than to an untyped file path.

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
