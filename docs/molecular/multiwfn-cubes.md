# Multiwfn cube workflows

CMW provides shell-first downstream workflows for validated molecular results.
These analyses are siblings of one another: FMO does not precede ESP, and
neither is added to the ORCA `opt+freq+sp` mode string.

## Validated source contract

`--source` identifies a version-1 CMW ORCA result manifest. The manifest must:

- be reusable, with successful execution and `VALID` scientific status;
- record intact `wavefunction` and `geometry` artifacts;
- have a target geometry identity equal to the strict XYZ geometry artifact;
- record `wavefunction_semantics.spin_mode`; and
- for FMO, record positive one-based `homo_index` and `lumo_index` values.

The preferred source is a finalized CMW ORCA result whose explicit artifacts
were recorded at ORCA finalization. Workflows never select the first file with
a convenient suffix. A standalone wavefunction with no validated result and
geometry lineage is intentionally rejected in this milestone.

For the supported FMO subset, `spin_mode` must be `restricted`, multiplicity
must be one, and the LUMO index must exceed the HOMO index. Unrestricted and
open-shell sources fail clearly because alpha and beta frontiers are not
silently collapsed.

## Runtime

Executable precedence is explicit option, Python configuration, `MULTIWFN_EXE`,
then an unambiguous `Multiwfn`/`multiwfn` on `PATH`. The automated menu profile
is currently supported for the demonstrably tested 3.8.x series; unknown or
other versions fail before a scientific launch.

Thread precedence is explicit option, `MULTIWFN_NTHREADS`, Python workflow
configuration, then the public default of 8. Contradictory supplied values fail
instead of being silently reconciled. For example:

```sh
MULTIWFN_NTHREADS=4 \
./scripts/workflows/generate_esp_cubes.sh \
  --source /path/to/validated-result.json \
  --output /path/to/analyses \
  --grid-spacing-bohr 0.20
```

CMW derives a read-only attempt-local `settings.ini`, records the source and
effective hashes, and never edits installation settings. A unique short alias
under `TMPDIR` is exposed as `Multiwfnpath` only for the child lifetime because
recent Multiwfn builds may truncate long configuration paths. Executable hash,
version, settings, requested threads, and process-health samples are execution
provenance, not scientific target identity.

Health sampling follows the owned process tree and selected output files.
`POSSIBLY_STALLED` and low observed core use are advisory diagnostics; neither
changes cube validity nor terminates a process.

## HOMO and LUMO cubes

Inspect a run without creating output:

```sh
./scripts/workflows/generate_homo_lumo_cubes.sh \
  --source /path/to/validated-result.json \
  --output /path/to/analyses \
  --grid-spacing-bohr 0.20 \
  --plan
```

Execution produces `homo.cube`, `lumo.cube`, an immutable attempt record, and a
target-level `result.json`. Each cube must have a complete header and scalar
payload and must reproduce the ordered source atoms and coordinates. The two
cubes must share dimensions, origin, axes, and geometry. Target identity
includes the source wavefunction hash, the explicit frontier indices, the
operation profile, and grid spacing.

## Density and ESP cubes

```sh
./scripts/workflows/generate_esp_cubes.sh \
  --source /path/to/validated-result.json \
  --output /path/to/analyses \
  --grid-spacing-bohr 0.20 \
  --plan
```

Execution produces `density.cube` and `esp.cube` as one paired scientific
product. Both cubes are validated independently and then compared for exact
dimensions and explicitly tolerant floating-point origin, axis, and atomic
geometry agreement. Visualization isovalues are not cube-generation defaults.

## Reuse and layout

Results are stored under `<output>/<operation>/<target-id>/`. Attempts are
append-only children of that target directory. Reuse requires the same source
wavefunction hash and scientific configuration plus structurally valid cube
files whose sizes and hashes still match. Changing a grid, frontier identity,
or source wavefunction creates a sibling target; older artifacts are retained.
