# Reviewed ORCA Jobs integration

The ORCA adapter reuses the existing typed target/geometry/resource models,
foreground `run_orca.sh`, sequential OPT/FREQ/SP workflow, native progress adapter,
and stage finalizer. Checking a specification neither invokes ORCA/MPI nor creates
an attempt. Intentional preparation creates a separate canonical attempt; it never
rewrites the imported input. Registration defaults to Hold. Release and controller
start are separate execution actions.

## Engine specification

The shared Jobs specification has `engine: "ORCA"`, explicit CPU, memory, MPI-rank
and thread commitments, a new absolute `workdir`, and these engine fields:

```json
{
  "runtime": {
    "executable": "/qualified/node/local/orca",
    "sha256": "<recorded executable SHA-256>",
    "version": "6.1.1",
    "qualification": "recorded",
    "contract": "/qualified/node/local/orca-runtime.json"
  },
  "scientific": {
    "profile": "sp",
    "input": "/selected/source/input.inp"
  }
}
```

`qualification: "synthetic"` labels benign executable acceptance. It does not skip
byte, geometry, source, resource or output checks and does not qualify a real engine.
Missing recorded identity/version is UNASSESSED, not execution eligibility. The
recorded driver is invoked directly, never under `mpirun`.

Single-stage profiles are `sp`, `opt` and `freq`. FREQ requires an explicit
`scientific.validation` object with `require_minimum` (boolean) and
`imaginary_tolerance_cm1` (finite nonnegative number). The other stages accept that
same optional policy. No minimum requirement or molecule-specific tolerance is
selected automatically.

For the existing compositions use:

```json
{
  "scientific": {
    "profile": "opt+freq+sp",
    "workflow": true,
    "structure": "/selected/source/geometry.xyz",
    "config": "/selected/source/workflow.json"
  }
}
```

The established workflow config must explicitly declare charge, multiplicity,
resources, validation policy and stage settings. The modes remain `opt`, `opt+sp`,
`opt+freq` and `opt+freq+sp`. The existing stage gates decide when a downstream stage
may materialize; no second scheduler is introduced. The initial workflow state and first prepared inputs are byte-pinned at final
launch admission; the existing workflow may then update its state as it executes.
A failed required FREQ result blocks SP. Both FREQ and SP retain the validated OPT geometry lineage.

## Supported input dialect and limits

CMW-generated and imported inputs use the same bounded check. Imported inputs must
contain exactly one matching SP/OPT/FREQ stage, explicit PAL (one keyword or one
`%pal nprocs N end` block), explicit positive integer `%maxcore`, and one Cartesian
angstrom geometry. Inline `* xyz charge multiplicity` coordinates require the
closing `*`; `* xyzfile charge multiplicity relative-file` references an exact
input-tree dependency, optionally quoted for spaces. Coordinates and ordered atom
rows use the existing strict XYZ model.

The first-class imported subset currently supports ordinary all-electron H–Kr
atoms; known HF/DFT keywords and STO-3G/def2 basis families are bounded by the
adapter's explicit vocabulary. Composite HF-3c, B97-3c and r2SCAN-3c do not require a
separate basis keyword. Supported extra blocks are `%scf MaxIter N end` and
`%geom MaxIter N end`. Electron-count/multiplicity checks apply only within this
all-electron subset.

Ghosts, ECPs, fragment-labelled atoms, unknown keywords, point charges, restart
orbitals, external basis/Hessian files and other dependency-bearing constructs are
UNSUPPORTED for this automated route. That is not a judgment of scientific
invalidity. No unknown syntax is deleted or silently repaired. The broader existing
preparation APIs and trusted generic command route retain their own contracts;
they are not described as validated by this bounded checker.

## Runtime and resources

Jobs CPU count and MPI ranks must equal effective PAL; this route has one thread
per rank. Its environment explicitly pins that thread policy and the selected
runner, avoiding inherited alternate runners or external-scratch redirection.
MaxCore remains MB per process, not a whole-job RSS cap. The explicit GiB commitment
must accommodate PAL × MaxCore using the existing 1024 MB/GiB and 80% usable-memory
policy (20% headroom). CMW never reduces scientific resource declarations to fit.

Recorded execution (serial or parallel) requires `runtime.contract`, an absolute,
target-local stored ORCA/MPI runtime contract. The pure checker verifies recorded
platform/architecture, driver, launcher, inspector, helper and library bytes without
running probes. Newly prepared runtime contracts include helper byte identities.
An older record without them cannot qualify the new first-class recorded route;
existing operational runtime validation remains backward compatible. The existing
runner still performs its established runtime/loader validation at actual launch.
A MacBook contract does not establish a mini's qualified runtime.

## Observation, results and controls

Single stages declare the exact future `stage.out` and native stage at registration.
It may be absent while held or starting: that is a pending known source. The runner
writes at that execution location; a final copyback path is never substituted.
For compositions, preparation records the exact workflow-state path and invokes
`run_opt_freq_sp.sh --state PATH`. The existing metadata resolver follows that exact
state as independently validated stages acquire their own native output paths.
The first stage's source is not frozen as a declaration for the whole composition.

The existing ORCA 6.1.x adapter retains DIIS/SOSCF, geometry cycles, native units,
criteria and stage boundaries. FREQ stage recognition does not invent displacement
progress. Files, History and Copy consume the same shared observations. The Jobs
attempt identity and scientific target/attempt association remain distinct.

The native runner finalizes each stage through the existing ORCA validator. Process
exit zero and Jobs Done do not by themselves establish scientific acceptance. A
nonconverged output is refused; FREQ follows the explicit selected policy. The
observer never finalizes or mutates results. `job.json` (single stage) or the exact
workflow result record carries the separate scientific result.

This new reviewed route does not enroll suspension. Existing explicitly enrolled
ordinary SP/OPT contracts remain available separately; FREQ and compositions do
not inherit them. Standard Jobs cancellation/ownership remain unchanged.

Shell assets are packaged from the existing source scripts. Installed runners clear
inherited checkout `PYTHONPATH` and use the selected installed Python; checkout
runners use only their own adjacent source tree. ORCA, MPI and licensed binaries are
never bundled. Software acceptance uses harmless substitutes: physical dual-Mac
and real-engine qualification remain pending.
