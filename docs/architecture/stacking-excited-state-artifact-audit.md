# Excited-state and fragment-resolved stacking artifact audit

## Scope and verdict

This audit covers the molecule-agnostic vertical-stacking path from a
periodic-derived structure registry through excited states, natural transition
orbitals (NTOs), and fragment-resolved hole/electron analysis. It reviews the
existing CMW workflow graph, typed artifacts, ORCA planning, Multiwfn command
adapter, provenance, and validation contracts. No external scientific program
was run.

**Verdict: conditionally production-ready.** After the additive extensions
described below, CMW can represent, validate, serialize, and propagate the
complete scientific lineage and the manuscript-level quantitative results. The
existing execution contracts can plan the ORCA optimization and TDDFT/TDA
steps and can plan a versioned Multiwfn invocation without a parallel runner.
Unattended production finalization still requires two adapter-level parsers
with real-version fixtures: an ORCA excited-state table parser and a Multiwfn
hole/electron metric parser. Until those parsers exist, quantitative result
objects must be created from independently parsed, explicit evidence rather
than inferred from normal process termination.

| Area | Before audit | After minimal extension | Production conclusion |
| --- | --- | --- | --- |
| Excited-state planning | Method, basis, root count, TDA flag, selection intent | Explicit functional, spin manifold, NTO settings, quantitative states, runtime, and rationale | Representation ready |
| NTO lineage | Typed parent and planned orbital output | State-resolved orbital pairs, weights, file roles, generator/version, and visualization | Representation ready |
| Hole/electron metrics | Centroids, distance, overlap, separation index, free-form contributions | Hole/electron extents, normalized populations, complete atom partition, structure/state identity | Representation ready |
| DAG and failure propagation | Complete typed path with generic dependencies | NTO is also an explicit required input to the analysis node when enabled | Ready |
| ORCA execution | Existing optimization and TDDFT/TDA stage contracts | Spin and NTO directives are rendered from explicit intent | Planning ready; result parser pending |
| Multiwfn execution | Existing runtime, stdin, command, and output discovery | Fragment partition and state identity are part of the scientific protocol | Planning ready; result parser pending |

## 1. ExcitedStateArtifact

### Audited gap

The original artifact recorded planning intent but could not preserve the
actual excitation energy and oscillator strength of each state, the ORCA
version that produced them, or why particular states were retained. A normal
termination and a count of printed states are not enough for manuscript-level
selection of a lowest bright, lowest transfer-like, or experimentally relevant
state.

### Quantitative contract

`ExcitedStateRecord` now represents one state using:

- a positive one-based state index;
- excitation energy in eV;
- non-negative oscillator strength;
- explicit singlet or triplet manifold;
- generic state-selection labels; and
- a required rationale whenever a state carries a selection label.

`create_excited_state_artifact` combines those records with a validated source
`StructureArtifact`. The finalized artifact records:

- source artifact identity and geometry hash;
- method, basis, and functional;
- TDDFT versus TDA, spin manifold, and requested number of roots;
- ORCA program and version;
- all quantitative state records;
- selected state indices and an overall selection rationale; and
- output files and runtime provenance.

The labels are deliberately open, molecule-independent strings. CMW records a
project's declared selection and its rationale; it does not decide whether a
state is chemically important.

The ORCA protocol renderer now includes explicit spin and NTO intent. ORCA 6.1
documents `%tddft NRoots`, `Triplets`, `DoNTO`, `NTOStates`, and `NTOThresh` as
TDDFT/TDA controls in its [TDDFT section](https://www.faccts.de/docs/orca/6.1/manual/contents/spectroscopyproperties/tddft.html).

## 2. NTOArtifact

### Audited gap

The original planned artifact had an excited-state parent and one generic
orbital path, but it did not identify which state or NTO pair an orbital
belonged to. It also lacked a finalized artifact constructor and fail-closed
validation.

### State-resolved contract

`NTOOrbitalRecord` identifies a state, pair number, non-negative pair weight,
and separate semantic file roles for the hole and electron orbitals.
`create_nto_artifact` records:

- the exact parent `ExcitedStateArtifact`;
- the NTO generation method;
- every state-resolved hole/electron orbital pair;
- concrete orbital file locations;
- program/version provenance; and
- generic visualization metadata such as isovalue or rendering references.

Validation rejects missing files, duplicate state/pair identities, states that
do not exist in the parent excited-state evidence, missing generator/version
metadata, or an invalid parent. The official ORCA description of natural
transition orbitals also defines them per excited state, consistent with this
[state-resolved identity](https://www.faccts.de/docs/orca/6.1/manual/contents/utilitiesvisualization/orca_nto_plot.html).

## 3. HoleElectronArtifact

### Quantitative descriptors

`HoleElectronMetrics` now supports all required geometry-independent values:

- hole and electron centroids in ångström;
- centroid separation distance in ångström;
- overlap and separation index;
- hole and electron extents in ångström; and
- normalized hole and electron populations keyed by generic fragment IDs.

The existing per-fragment contribution mapping remains readable. Canonical
`hole_population` and `electron_population` mappings are additive fields and
must agree with the legacy contribution representation when both are present.
Each population is finite, within `[0, 1]`, and sums to one within numerical
tolerance.

### Fragment and state contract

`FragmentDefinition` stores a generic fragment identifier and explicit
zero-based atom indices. A fragment-resolved protocol requires at least two
unique fragments whose atom sets are non-overlapping and exactly cover the
source structure. It does not assign chemical meaning to any fragment.

A finalized fragment-resolved `HoleElectronArtifact` additionally records:

- the source `StructureArtifact`, geometry hash, and partition hash;
- the analyzed excited-state index;
- the complete indexed fragment partition;
- the parent `ExcitedStateArtifact` and optional `NTOArtifact`;
- the Multiwfn menu contract, runtime version, executable, output roles, and
  visualization settings; and
- detailed validation checks for geometry, state, fragment, metric, and parent
  compatibility.

Finalization fails closed when the partition is missing or invalid, fragment
population keys differ from the partition, the requested state is absent from
the excited-state or supplied NTO evidence, or the source geometry identity has
changed.

## 4. DAG lineage and failure propagation

The existing graph can express the full transitive lineage:

```text
StructureArtifact
  -> StackingTemplateArtifact
  -> DimerStructureArtifact
  -> ConstrainedOptimizationArtifact / OptimizationArtifact
  -> ExcitedStateArtifact
  -> NTOArtifact
  -> HoleElectronArtifact
```

The concrete workflow also retains the optimized `StructureArtifact`, optional
frequency validation, and ground-state wavefunction needed by ORCA. Nodes use
typed `ArtifactRequirement` declarations. When NTO generation is enabled, the
hole/electron node now explicitly requires both the excited-state and NTO
artifacts.

`validate_stacking_artifact_lineage` rejects missing, duplicate, orphan, or
non-upstream parents. Generic artifact compatibility rejects invalid parents.
The core graph's topological validation and batch finalizer already stop
dependent artifacts when a parent is absent or invalid, so no stacking-specific
execution or failure engine is needed.

The new strict metadata checks are versioned by additive contract markers.
Legacy planned artifacts and schema-version-1 manifests remain readable;
quantitative finalized artifacts cannot silently omit manuscript evidence.

## 5. Execution-contract assessment

### ORCA

Optimization, single-point, frequency, and excited-state tasks already share
`OrcaStageSpec` and `ExecutionIntent`. The TDDFT/TDA stage remains an ORCA
single-point operation while retaining a distinct scientific task and
`ExcitedStateArtifact` result type. Explicit spin and NTO directives are now
derived from protocol intent rather than method-name inference.

CMW's current output parser validates termination, SCF convergence, protocol
identity, and a minimum excited-state count. It does **not** yet parse a
version-aware table of excitation energies and oscillator strengths. ORCA 6.1
changed parts of the printed absorption-spectrum format, so production parsing
should be fixture-driven rather than based on an unverified regular expression.

### Multiwfn

The existing `MultiwfnCommandSpec`, runtime identity, deterministic stdin,
output discovery, and attempt layout are sufficient operational abstractions.
The fragment definition belongs to the scientific `HoleElectronProtocol` and
is passed through that existing adapter boundary. Command construction now
also verifies that the actual stdin file exactly matches the planned menu
sequence.

CMW does **not** yet render a fragment mapping into a version-specific Multiwfn
menu or parse a real Multiwfn quantitative hole/electron report into
`HoleElectronMetrics`. Those are adapter concerns, not reasons to add a second
executor. Production support should add a paired versioned renderer/parser with
captured fixtures and feed its normalized result to the artifact factory.

## 6. Recommended next changes

The following work is intentionally not implemented in this audit because no
real-version output fixtures were provided:

1. Add an ORCA-version-aware excited-state parser that returns state index,
   spin, excitation energy, and oscillator strength from representative ORCA
   outputs, including the 6.1 format.
2. Add a Multiwfn-menu-contract-aware renderer/parser pair. The renderer must
   encode the declared fragment partition; the parser must recover centroids,
   extents, overlap, separation index, and fragment populations.
3. Connect both parsers to normal attempt finalization so the new factories
   receive evidence only after execution, protocol, and file validation pass.
4. Validate the parsers against small stored text fixtures before authorizing
   unattended production runs. No scientific executable is needed for those
   regression tests.

## 7. Compatibility and synthetic validation

The artifact schema version remains unchanged. New protocol fields have
defaults, new result fields are additive, and the stronger core compatibility
rules activate only for artifacts carrying the new quantitative contract
markers. Existing OPT, FREQ, SP, CP/LED, IGMH, and legacy stacking workflows are
not reinterpreted.

Synthetic tests cover:

- quantitative `ExcitedStateArtifact` creation and missing selection evidence;
- TDDFT/TDA execution intent plus explicit NTO rendering;
- state-resolved `NTOArtifact` lineage, orbital files, and provenance;
- normalized generic fragment hole/electron populations and spatial extents;
- incomplete and overlapping fragment maps;
- missing requested-state evidence;
- typed DAG lineage and deterministic execution layouts; and
- reuse of the existing Multiwfn command/runtime contract.

All values in these tests are explicitly synthetic and are not scientific
results.

Validation result for this audit:

- focused vertical-stacking suite: 13 tests passed;
- complete repository regression suite: 221 tests passed; and
- no ORCA or Multiwfn execution was performed.
