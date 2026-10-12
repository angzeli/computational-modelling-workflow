# Provenance and ORCA execution lifecycle

The runtime distinguishes a **job target** from an **execution attempt**. A
target hashes scientific intent: stage type, geometry identity, charge,
multiplicity, and normalized ORCA calculation settings. An attempt records the
resources, executable evidence, exact generated input, time, and optional
parent attempt used to realize that target. Resource changes can therefore
create another attempt without pretending the scientific target changed.

Each attempt manifest is schema-versioned and atomically written. It records
the target, geometry lineage, execution and scientific states, factual ORCA
evidence, optional Git evidence, and SHA-256 artifact records. Git is optional;
a calculation directory need not be in a repository.

Reuse is fail-closed. Target identity, successful execution, valid scientific
status, every required artifact, its size, and its hash must agree. A file's
mere existence or an ORCA normal-termination marker is insufficient. A
non-reusable directory is not overwritten: callers must select a distinct
attempt directory.

Local locks are scoped to a scientific stage target across all of its attempts
and contain host, PID, token, and job identity. Active local ownership blocks
mutation. Stale local ownership is diagnosed and can be
replaced only explicitly. Unreadable or remote ownership remains uncertain and
blocks mutation. These local locks do not solve scheduler or shared-filesystem
coordination.

`scripts/orca/run_orca.sh` owns executable discovery, locks, scratch space,
stdin isolation, file descriptors, process launch, signal forwarding, and
cleanup. It communicates with Python through JSON and stable exit codes. Python
owns target identity, ORCA evidence, validation, provenance, and reuse. Optional
macOS `caffeinate` support is enabled only with `CMW_CAFFEINATE=1`; it is never
required for correctness.

The runner resolves paths for provenance and validation, then starts ORCA from
the input file's directory using only the local input filename. This keeps the
ORCA process contract stable when an execution layout contains whitespace while
retaining absolute paths in CMW metadata.

Parallel production launches additionally consume a validated
`orca-runtime.json`. The runtime contract pins the configured MPI launcher,
executable and library search roots, ORCA executable, and the transitive
dynamic libraries used by ORCA's parallel helpers. The contract also records
verified OpenMPI Fortran datatype capabilities so an ABI-incomplete MPI build
cannot pass based on version and library names alone. The runner revalidates this
identity before acquiring execution state, prepends the recorded `PATH` and
platform library-path variable, and fails before launching ORCA if the contract
is missing, stale, or incomplete.

On macOS, `DYLD_*` variables can be removed when ORCA launches MPI through a
protected system shell. CMW therefore does not treat `DYLD_LIBRARY_PATH` as
sufficient launch evidence. Bare dynamic-library dependencies are recorded in
the runtime contract and materialized as verified symlinks in the attempt
working directory, which is an explicit dyld search location for ORCA's MPI
helpers. Before scientific execution, CMW launches `orca_startup_mpi` without
calculation input through `/bin/sh` and rejects dyld loading failures. The
result is stored as `orca-runtime-launch.json`; collisions, changed sources,
missing links, and failed loader probes all stop before ORCA receives the input.

## Explicit controlled suspension enrollment

New local Jobs attempts may select backend `orca-local-v1` and pass
`--controlled-suspension` to `scripts/orca/run_orca.sh`. Both are required:
an environment variable or runner flag alone is not a successful enrollment.
The runner validates the private version-1 context, exact attempt, claim,
worker and pinned session anchor, then registers its own birth identity and
its dedicated computational driver group. No running legacy attempt is adopted.
The coordinator and ownership anchor remain runnable; they are outside the
computational domain that the Jobs worker controls.

The initial input scope is deliberately bounded: CMW-rendered ordinary SP/OPT,
an explicit small set of ordinary SCF/DFT keywords and basis forms, and no extra
input blocks. The input must still match its target, electronic state, geometry
and declared PAL count. FREQ/NumFreq, task farms, compound jobs, unknown keywords
or blocks, and `CMW_CAFFEINATE=1` refuse enrollment. This is a topology support
limit, not a judgment about those scientific methods. No scientific input is
rewritten to make it eligible. Serial operation is supported by the same driver
contract; PAL additionally requires the existing validated schema-3 ORCA runtime
contract. MPI identity and control executables are registered separately from
scientific progress; native MPI qualification does not qualify the ORCA engine.

ORCA itself remains the serial driver: it is never launched under `mpirun`.
The worker first quiesces computational producers, uses the qualified MPI signal
forwarding route for active parallel modules, and verifies computational members
are OS-stopped. Required MPI controls may remain runnable. The runner keeps
waiting for true process termination. Stop observations cannot trigger copy-back,
finalization, scratch cleanup or the next workflow stage. A newly enrolled driver
runs in a separate group in the existing owned session, preserving Python 3.10
compatibility. Escaping-session and unobservable/dynamic unsupported domains fail
closed; process-tree sampling is not an atomic kernel freeze.

The supported ORCA runner has **no configured execution timeout** and acknowledges
that known policy explicitly. This differs from an unknown external watchdog.
Scratch directories, local MPI session storage, executable location and attempt
source directories are registered by directory identity and must remain available
for Continue. RAM, file descriptors and admission commitments remain held. The
suspension clock includes sleep (`mach_continuous_time` on macOS;
`CLOCK_BOOTTIME` on Linux), but physical sleep/wake remains separately unqualified.
No `safe to close lid`, disk-ejection or reboot-recovery guarantee is made.

The checkout workflow accepts `--controlled-suspension` for its single `opt`
mode only. Multi-stage OPT/SP and all frequency workflows keep their existing
execution behavior and currently refuse that option, because a later runner
cannot inherit the first runner's exact-attempt registration. A future stage
handoff requires its own verified contract; it is not automatic.

Use the Jobs Suspend/Continue requests for explicit manual control. Dispatch
pause/resume remains separate. Cancellation is termination, including a narrowly
verified CONT when required to deliver pending TERM; it is never a scientific
restart. Actual ORCA engine and physical sleep acceptance are pending separate
approval, even when software and harmless native MPI tests pass.
