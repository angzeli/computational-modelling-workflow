# Execution resource profiles

CMW accepts an optional versioned execution-resource document. It selects one
named profile and translates only runtime concerns for supported programs:

```yaml
schema_version: 1
active_profile: local_mac
profiles:
  local_mac:
    orca:
      nprocs: 8
      total_memory_gb: 18
      mpi:
        bin_directory: /absolute/path/to/openmpi/bin
        library_directories:
          - /absolute/path/to/openmpi/lib
    multiwfn:
      nthreads: 8
      total_memory_gb: 18
```

The schema is generic: profile names are user data, not machine names known to
CMW. The active profile, both program sections, and all positive resource
values are required. Missing, malformed, non-positive, or unknown selections
fail closed. Workflows that do not supply this optional document retain their
existing resource interfaces and defaults.

Parallel ORCA profiles may also declare an MPI runtime. These machine-specific
paths are execution metadata, not scientific method settings. Paths must be
absolute; CMW does not guess between multiple MPI installations or silently
fall back to a different version.

## Scientific identity and execution identity

Methods, basis sets, structures, charges, multiplicities, scientific protocol,
and artifact lineage define scientific identities. A resource profile defines
an execution identity. Its stable `execution_profile_hash`, selected resource
values, and source path are stored in plan or attempt provenance; they are not
added to `JobTarget`, ORCA scientific protocol, artifact identity, or DAG
identity. Consequently, changing processor, thread, or memory allocations does
not invalidate an otherwise identical scientific target.

## ORCA translation

ORCA receives the selected `nprocs` through `%pal`. Because ORCA `%maxcore` is a
per-process allocation, CMW derives it from the total budget with an explicit
conservative policy:

```text
total_memory_mb = floor(total_memory_gb * 1024)
usable_memory_mb = floor(total_memory_mb * 0.80)
maxcore_mb_per_process = floor(usable_memory_mb / nprocs)
```

The integer result must be positive, and
`nprocs * maxcore_mb_per_process` must not exceed the declared total budget.
For the example 18 GB, 8-process profile, CMW emits `%maxcore 1843`; the derived
ORCA allocation is 14,744 MB, below both the 80% usable allowance and the
18,432 MB declared budget. The unused margin is reserved for program and
operating-system overhead.

## ORCA MPI runtime contract

Before a parallel production launch, `cmw.molecular.orca.cli runtime-prepare`
resolves the configured `mpirun`, records its version and hash, inspects ORCA's
parallel helper binaries, and resolves their dynamic-library dependencies
transitively. It also interrogates `ompi_info` and requires the size-specific
integer, real, and complex MPI datatypes used by ORCA's parallel helpers. An
OpenMPI build with its Fortran datatype table disabled is rejected even when
its version, launcher, and shared-library names otherwise match. On macOS this
includes bare dependencies such as
`libmpi.40.dylib`; on Linux it uses the corresponding ELF linkage report.
Missing launchers, directories, helper binaries, or libraries fail with
`FAILED_RUNTIME_CONTRACT` before ORCA is started.

The resulting `orca-runtime.json` records the exact `PATH` and dynamic-library
prefixes, executable identities, resolved libraries, and a stable runtime ID.
The shell runner revalidates the contract immediately before execution and
prepends the recorded paths. On macOS it additionally materializes verified
bare MPI libraries in the execution working directory and performs an actual
loader-only probe through the same system-shell boundary ORCA uses. Both the
static runtime contract and launch-time `orca-runtime-launch.json` are stored
with attempt provenance. This avoids relying on `DYLD_LIBRARY_PATH`, which
macOS can strip before ORCA's MPI helper is loaded.
Production entrypoints should pass both `--runtime-contract` and
`--require-runtime-contract`. Legacy serial and test entrypoints remain
readable, but cannot satisfy the strict parallel-production gate without a
validated runtime contract.

## Multiwfn translation

Multiwfn `nthreads` is passed into the existing attempt-local `settings.ini`
and runtime provenance path. The profile's total memory is recorded as
execution provenance only. CMW does not invent a Multiwfn memory directive or
claim that the total budget is enforced by a native keyword.

Explicit CLI or existing configuration values that contradict a selected
profile fail rather than silently overriding it. No external executable is
launched while a plan is being created.
