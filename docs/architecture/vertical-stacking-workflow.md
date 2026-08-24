# Reusable vertical stacking workflow

## Motivation and scope

`cmw.molecular.stacking` expresses a periodic-structure-informed vertical
molecular-pair workflow without selecting a molecule, functional, basis,
periodic code, or chemical interpretation. A project supplies the molecular
partitions, ordered core mappings, electronic-structure protocols, and
Multiwfn menu contract. CMW measures and preserves those declarations, builds
typed artifacts, and creates a non-executing workflow plan.

The package deliberately does not infer an aromatic core, discover a preferred
dimer, or decide whether a calculated state is chemically important. Those are
project-level scientific choices. The reusable contract begins after a project
has selected an explicit periodic pair and explicit generic core atoms.

## Structural abstractions

### Periodic pair definition

`PeriodicPairDefinition` declares two non-overlapping molecular atom
partitions and an ordered core mapping for each component. Indices are
zero-based. A core is any non-collinear ordered set of at least three atoms;
it is not required to be aromatic.

The source `StructureArtifact` must be validated and must declare
`metadata.periodic: true`. `extract_stacking_template` then constructs a
deterministic local frame for each mapped core and records:

- the source artifact, file, format, method, and geometry hash;
- periodic-origin metadata supplied by the project;
- molecular-pair identities and source atom partitions;
- source and monomer-local core indices;
- the oriented stacking axis;
- plane and centroid separations in ångström;
- an in-plane slip vector in the first core's local frame;
- relative in-plane rotation in degrees; and
- extraction method and source-hash provenance.

The result is a `StackingTemplateArtifact`. Missing periodic identity,
out-of-range or overlapping mappings, collinear cores, and incomplete
provenance fail closed.

### Vertical dimer assembly

`assemble_vertical_dimer` consumes one or two validated monomer
`StructureArtifact` objects and a validated template. Monomer identities must
match the template exactly. Component A retains its input coordinates;
component B is transformed into the template registry using deterministic
local frames.

The resulting `DimerStructureArtifact` stores components in A-then-B atom
order and records each component's source artifact, output atom range, local
core mapping, template identity, charge, multiplicity, ordered-element
identity, and persisted geometry hash. Validation re-reads the output file and
checks atom identity, parent identity, plane separation, centroid separation,
slip, and rotation. This validated XYZ-backed `StructureArtifact` is directly
usable by the existing ORCA geometry contract.

## Relaxation and electronic-analysis contracts

`RelaxationProtocol` describes a hierarchy rather than a molecule:

1. `ConstrainedOptimizationArtifact` — an ORCA optimization with explicit,
   non-overlapping fixed atom regions. The ORCA constraint block is generated
   deterministically from the zero-based mappings.
2. `OptimizationArtifact` — an optional full optimization whose plan contains
   no fixed regions.
3. `FrequencyArtifact` — an optional validation branch on the final optimized
   structure.

The constrained artifact retains the complete fixed-region contract and the
input dimer parent. The full optimization retains both the constrained result
and its generated structure as parents.

`ExcitedStateProtocol` adds an explicit `excited_state` computational task to
the ORCA execution contract. TDDFT and TDA plans require a positive number of
roots, explicit state-selection criteria, and a `%tddft` block. ORCA still
performs an SP operation, but it cannot be confused with a ground-state SP:
the stage type, task, required behavior, output validation, and
`ExcitedStateArtifact` are distinct. A finalized quantitative artifact records
method, basis, functional, spin manifold, program/version, number of roots,
state energies and oscillator strengths, selection labels and rationale,
source structure, and source geometry hash. Optional natural transition
orbitals produce a state-resolved `NTOArtifact` parented by the excited-state
result.

`HoleElectronProtocol` supplies a versioned, explicit Multiwfn menu contract,
state index, deterministic input stream, semantic output specifications, and
optional visualization metadata. It is translated through the existing
Multiwfn command/runtime layer; the stacking package does not launch or
reimplement Multiwfn. The resolved runtime menu contract must match the planned
protocol, and required output files must exist and be non-empty before
finalization. A finalized `HoleElectronArtifact` records:

- the parent `ExcitedStateArtifact` and optional `NTOArtifact`;
- Multiwfn executable and version provenance;
- the complete analysis protocol and declared output roles;
- hole and electron centroids in ångström;
- their separation distance, overlap, separation index, and spatial extents;
- normalized hole and electron populations keyed by generic fragment IDs;
- a complete, non-overlapping source-atom partition; and
- visualization metadata.

## Workflow graph and artifact lineage

`prepare_vertical_stacking_workflow` performs only deterministic structure
extraction/assembly and plan construction. External scientific programs are
not executed. The generated generic `WorkflowGraph` is:

```text
periodic StructureArtifact             monomer StructureArtifact(s)
             │                                      │
             ▼                                      │
StackingTemplateArtifact ───────────────────────────┘
             │
             ▼
 DimerStructureArtifact
             │
             ▼
ConstrainedOptimizationArtifact + StructureArtifact
             │
             ▼
 OptimizationArtifact + StructureArtifact
             │
             ├──────────────► optional FrequencyArtifact
             │
             ▼
SinglePointArtifact + WavefunctionArtifact
             │
             ▼
   ExcitedStateArtifact
             │
             ├──────────────► optional NTOArtifact
             │                         │
             └─────────────────────────┘
                         │
                         ▼
              HoleElectronArtifact
```

Every dependency is declared through the existing CMW DAG. Planned artifact
lineage is checked for undeclared types, missing parents, non-upstream parents,
duplicate identities, and intermediate orphan artifacts. ORCA nodes use
`OrcaStageSpec` and `ExecutionIntent`; executable nodes receive deterministic
`ExecutionLayout` instances when an absolute project root is supplied.
Multiwfn analysis retains the existing runtime-integrity and output-discovery
boundary.

## Example protocol

The adapter or project layer resolves named atom groups, method definitions,
and menu sequences before constructing the Python models. A YAML-facing domain
configuration can use the following molecule-agnostic shape:

```yaml
workflow:
  stacking:
    template:
      source: periodic_structure
      extraction_method: ordered_core_plane_fit_v1
      indexing: zero_based
      pair:
        molecule_identities: [monomer, monomer]
        molecule_atom_indices:
          A: [0, 1, 2, 3]
          B: [4, 5, 6, 7]
        core_atom_indices:
          A: [0, 1, 2]
          B: [4, 5, 6]

    assembly:
      type: vertical_dimer

    relaxation:
      constrained:
        fixed_regions:
          - id: core_A
            atom_indices: [0, 1, 2]
          - id: core_B
            atom_indices: [4, 5, 6]
      full:
        enabled: true

    ground_state:
      method: PBE0
      basis: def2-SVP
      frequency: true

    excited_state:
      enabled: true
      method: PBE0
      basis: def2-SVP
      theory: TDA
      spin_manifold: singlet
      number_of_roots: 10
      state_selection:
        states: [1, 2]
      nto:
        enabled: true

    hole_electron:
      enabled: true
      state_index: 1
      menu_contract: explicit-versioned-contract
      fragments:
        indexing: zero_based
        definitions:
          - fragment_id: fragment_1
            atom_indices: [0, 1, 2, 3]
          - fragment_id: fragment_2
            atom_indices: [4, 5, 6, 7]
      outputs:
        - role: hole_density
          raw_path: hole.cub
          output_path: analysis/hole.cub
        - role: electron_density
          raw_path: electron.cub
          output_path: analysis/electron.cub
```

## Backward compatibility

The artifact schema version remains 1 and existing manifests remain readable.
All artifact registrations are additive. Existing OPT, FREQ, and SP stage
types, task mappings, render behavior, CLIs, HOF adapter workflows, and
Multiwfn workflows are unchanged. TDDFT is an additional stage and
`excited_state` is an additional computational task; neither is inferred from
a method name.
