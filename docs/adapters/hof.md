# Hydrogen-bonded HOF adapter

`cmw.adapters.hof` is a domain adapter. It translates the HOF project's
`systems.yaml`, `methods.yaml`, and `protocol.yaml` documents into generic CMW
workflow nodes, typed artifact templates, and non-executing ORCA input plans.
An optional `execution.yaml` selects a generic CMW resource profile; it does not
change the HOF scientific schema or chemistry.
No HOF rule is implemented in `cmw.core`.

## Schema mapping

| HOF source | Adapter model | Generic CMW output |
| --- | --- | --- |
| `systems.<id>` | `HofSystem` | stable system identity and node configuration |
| `fragments` | `HofFragment` | fragment calculation nodes and ghost-atom plans |
| `hydrogen_bonds` | `HofHydrogenBond` | validated domain metadata |
| `interaction_energy.method_ref` | `HofInteractionProtocol` | resolved ORCA method and protocol metadata |
| workflow outputs | `HofInteractionPlan` | DAG plus typed artifact templates |
| optional `execution.yaml` | `ExecutionProfile` | ORCA and Multiwfn plan resources and execution provenance |

External atom indices may be zero- or one-based, as declared by
`atom_index_base`. The adapter normalizes them to zero-based indices for stable
internal identity and converts them back when serializing the source-facing
system record. Relative structure paths resolve against the HOF project root;
callers may provide an explicit project root or structure override.

Method references are resolved recursively. Missing references, reference
cycles, conflicting frozen-fragment settings, non-ORCA programs, disabled CP or
LED, and LED protocols without a DLPNO method, basis, and PNO setting fail
closed.

## HOF validation

The adapter requires exactly two non-empty fragments. Atom indices must be in
range, unique within a fragment, non-overlapping between fragments, and must
cover the complete ordered geometry. Fragment charges must sum to the system
charge.

Every declared hydrogen bond is checked for:

- an existing N/O/S donor atom;
- an existing hydrogen atom whose element is H;
- an existing compatible acceptor atom;
- donor and hydrogen membership in the same fragment;
- acceptor membership in the opposite fragment;
- a unique bond identifier and the declared expected count; and
- consistent atom-role, fragment-direction, and optional symmetry metadata
  when the bonds are marked symmetry-equivalent.

These rules live only in `cmw.adapters.hof.validation`.

## Interaction graph and artifacts

The builder emits the following generic DAG:

```text
                         dimer
                   DimerEnergyArtifact
                  WavefunctionArtifact
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
       fragment_<id>            fragment_<id>
  FragmentEnergyArtifact   FragmentEnergyArtifact
              └───────────┬───────────┘
                          ▼
                  CPInteractionArtifact
                          │
                          ▼
                      LEDArtifact
```

Fragment identifiers are project data; the builder does not attach semantics
to names such as `A`, `B`, `left`, or `right`. The CP node is a generic
`AggregationNode`, and LED is a `DerivedResultNode`. Artifact templates start
with `UNKNOWN` validation because planning does not claim a completed
calculation.

The dimer node exposes separate energy and wavefunction artifact identities.
This keeps the CP energy protocol comparable to the fragment energy protocols,
while the wavefunction lineage records that LED and explicit fragments were
requested.

The current interaction builder records a requested deformation-energy output
in plan metadata but does not invent deformation calculations or an artifact
type. That workflow can be added later inside this adapter when its scientific
input contract is explicit.

## ORCA input plans

`build_hof_orca_calculations()` creates one dimer and one calculation per
fragment without launching ORCA. The dimer input uses ORCA fragment labels such
as `O(1)` and `O(2)` and enables the `LED` keyword. Each counterpoise fragment
input preserves the complete dimer geometry and marks atoms outside the active
fragment as ghost atoms using ORCA's `O:` syntax.

The method, basis, PNO, TightSCF, LED, fragment-count, CP, active/ghost atom,
system identity, charge, and multiplicity metadata remain inspectable in the
plan.

```python
from pathlib import Path

from cmw.adapters.hof import load_hof_configuration, build_hof_interaction_workflow

root = Path("/path/to/h_bond")
configuration = load_hof_configuration(
    systems_path=root / "config/systems.yaml",
    methods_path=root / "config/methods.yaml",
    protocol_path=root / "config/protocol.yaml",
    execution_path=root / "config/execution.yaml",  # optional
    system_id="hof_h4tbapy",
)
plan = build_hof_interaction_workflow(configuration)
print(plan.graph.topological_order())
```

All adapter tests use synthetic geometries and fixtures. They do not invoke
ORCA.

The optional execution document is validated generically. An unknown
`active_profile`, including a name that differs from all keys under `profiles`,
fails closed. Selected ORCA resources use CMW's conservative per-process
`%maxcore` derivation, while Multiwfn receives only its thread count through the
existing runtime layer. Resource and profile hashes remain outside calculation
and artifact identity.
