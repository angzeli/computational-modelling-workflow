# ORCA intermolecular LED

CMW implements one production contract for standard fixed-geometry,
two-fragment ORCA local energy decomposition (LED):

```text
StructureArtifact
  ├─ dimer DLPNO-CCSD(T) LED single point
  ├─ fragment A in the full dimer basis (partner atoms are ghosts)
  └─ fragment B in the full dimer basis (partner atoms are ghosts)
           │
           └─ numerical LED assembler ──> validated LEDArtifact
```

The versioned reference semantics are
`FROZEN_FRAGMENT_IN_DIMER_BASIS`. The two reference calculations retain the
complete dimer coordinates and basis, make only the selected fragment active,
and mark every partner atom as a ghost. They are intrinsic numerical parents
of the six-component `LEDArtifact`; CMW does not create or require a
`CPInteractionArtifact` for this workflow.

This grammar and reconstruction are validated only for ORCA 6.1.1. Unknown
versions and unknown stored grammar identifiers fail closed.

## Scientific input and identity

The source must be a finalized, valid `StructureArtifact`. Exactly two explicit
`FragmentDefinition` records must be supplied. CMW requires the fragments to be
disjoint and to cover every zero-based atom index; they need not be contiguous.
CMW never infers fragments from coordinates.

Each fragment-reference identity hashes all of the following:

- source `StructureArtifact` identity and full-dimer geometry hash;
- full fragment partition and fragment identity;
- active and ghost atom mappings;
- fragment charge and multiplicity;
- method, basis, PNO, reference approximation, ORCA blocks, and all other
  scientific protocol inputs; and
- reference-semantics version.

Consequently, references are reusable only when all scientific inputs and the
source output hashes match exactly. A shared chemical name is never sufficient
for reuse. Absolute paths, resource allocation, and report formatting are not
scientific identities.

The dimer renderer preserves the validated input form:

```text
! DLPNO-CCSD(T) def2-TZVPP def2-TZVPP/C def2/JK RIJK TightPNO TightSCF LED SP
```

Fragment labels are applied only at the ORCA renderer boundary. Fragment
reference inputs use the same frozen coordinates, omit `LED`, and use ORCA
ghost labels for the partner atoms. There is no optimization, frequency,
deformation, association, COVALED, fp-LED, LEDAW, IGMH, or excited-state branch.

## Numerical reconstruction

CMW preserves the raw ORCA dimer quantities and both raw reference results in
Hartree. The manuscript-level components are assembled as:

```text
electronic preparation = sum(dimer intra-fragment REF)
                         - sum(fragment-reference E(0))

electrostatics          = dimer Electrostatics (REF.)
exchange                = dimer Exchange (REF.)

non-dispersive CCSD     = dimer non-dispersion strong + weak pairs
                         - sum(fragment-reference corrected correlation)

dispersion              = dimer dispersion strong + weak pairs

triples                 = dimer triples
                         - sum(fragment-reference triples)
```

The reconstructed historical LED interaction total is the sum of these six
components. An independent three-calculation total is also evaluated as:

```text
E(dimer) - E(fragment A in dimer basis) - E(fragment B in dimer basis)
```

The residual is the six-component sum minus this independent total. The
default acceptance tolerance is `1e-6 Eh`: the real ORCA 6.1.1 outputs have
about nine decimal places in component rows and residuals near `3e-7 Eh`, so
machine epsilon would not be scientifically appropriate.

ORCA's dimer-only `Total interaction` is retained under the explicit raw name
`orca_dimer_direct_total_interaction_hartree`. It is not the historical
three-calculation total and must never be substituted for it.

Derived kJ/mol values use CMW's single Hartree conversion constant. The parser
does not attach mechanistic interpretations to component magnitudes.

## Historical regression evidence

Reduced fixtures retain the real ORCA 6.1.1 grammar and values from completed
H4TBAPy and H4TABAPy calculations. Exact reconstruction from the three source
outputs gives:

| system | preparation | electrostatics | exchange | non-dispersive CCSD | dispersion | triples | total | residual (Eh) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| H4TBAPy | 594.936507 | -587.141301 | -83.430360 | 17.886534 | -24.380421 | -2.315544 | -84.444585 | 2.935059e-7 |
| H4TABAPy | 542.201571 | -532.417862 | -77.654959 | 17.325341 | -24.176554 | -2.061947 | -76.784410 | 3.018020e-7 |

Component and total columns are kJ/mol. The older curated H4TBAPy table reported
`-84.52 kJ/mol` because its fragment-B terms duplicated fragment A. The actual
fragment-B ghost calculation is slightly different. CMW deliberately anchors
the exact source calculations, records this discrepancy, and does not reuse a
reference merely because both fragments have the same chemical name.

## Lifecycle and command interface

`cmw.molecular.workflows.intermolecular_led_cli` provides three generic
operations:

- `plan` consumes a structure artifact, fragments, fragment electronic states,
  and an ORCA stage specification and writes a normal CMW `ExecutionPlan`;
- the existing `cmw.molecular.orca.cli materialize-plan` command renders each
  of the three calculation nodes through `WorkflowPlanMaterializer` and the
  standard `ExecutionLayout`;
- `finalize` consumes the three normally finalized ORCA result manifests,
  parses their existing outputs, and atomically writes the numerical
  `LEDArtifact`; and
- `reuse` revalidates exact lineage, contracts, attempts, executable identity,
  numerical content, and output hashes without running ORCA.

The numerical assembler is not an executable scientific job. It remains gated
until all three parent calculations are valid. If parsing or reconstruction
fails, no `LEDArtifact` is finalized, but the successful ORCA attempts remain
available for later refinalization without rerunning DLPNO-CCSD(T).

Local macOS operation uses the existing shell-owned launch, stop/resume, lock,
disk-floor, and resource-profile machinery. `nprocs`, declared memory, derived
`MaxCore`, and disk safety remain configurable; the LED workflow does not
change method quality to fit resources.

## Limitations

- Only the observed ORCA 6.1.1 two-fragment intermolecular grammar is validated.
- The non-ghost isolated-fragment convention is a distinct, currently
  unvalidated semantics and is not a production workflow here.
- CP interaction reporting, deformation, association, optimization, COVALED,
  fp-LED, and LEDAW are outside this contract.

LED interaction decomposition on a fixed geometry does not by itself provide
gas-phase association thermodynamics or experimental population ratios.
