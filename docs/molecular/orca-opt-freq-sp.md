# Configurable ORCA OPT / FREQ / SP workflow

The first reusable molecular milestone takes a strict XYZ structure through
one of four run-time modes:

| Mode | Sequential stages | Skipped stages |
| --- | --- | --- |
| `opt` | OPT | FREQ, SP |
| `opt+sp` | OPT → SP | FREQ |
| `opt+freq` | OPT → FREQ | SP |
| `opt+freq+sp` | OPT → FREQ → SP | none |

Every required stage must be scientifically valid before the next stage can
start. In `opt+freq+sp`, an invalid FREQ result blocks a new SP attempt. Both
FREQ and SP take their geometry from the validated OPT artifact; FREQ is not a
geometry-producing parent.

## Plan and run

The public interface is the shell workflow:

```sh
./scripts/workflows/run_opt_freq_sp.sh \
  --mode 'opt+freq+sp' \
  --structure molecule.xyz \
  --config workflow.json \
  --output runs/molecule \
  --plan
```

Remove `--plan` to execute. `--plan --json` returns the same plan in a
machine-readable form. Planning does not create the output directory or launch
ORCA. It reports selected and skipped stages, dependencies, geometry sources,
calculation identities, resources, output location, validation policy, and any
reuse decision that can already be established safely.

## JSON configuration

JSON was chosen because the package supports Python 3.10 and keeps a
dependency-free runtime; standard-library TOML parsing is only available from
Python 3.11. The configuration schema is deliberately smaller than an ORCA
input language:

```json
{
  "schema_version": 1,
  "charge": 0,
  "multiplicity": 1,
  "orca_exe": "orca",
  "resources": {
    "nprocs": 2,
    "maxcore_mb_per_process": 1000
  },
  "validation": {
    "require_minimum": true,
    "imaginary_tolerance_cm1": 5.0
  },
  "stages": {
    "opt": {"keywords": "HF STO-3G", "blocks": []},
    "freq": {"keywords": "HF STO-3G", "blocks": []},
    "sp": {"keywords": "PBE0 def2-SVP", "blocks": []}
  }
}
```

OPT, FREQ, and SP settings are independent. The package appends the relevant
stage keyword but does not choose a functional, basis, approximation,
dispersion correction, solvent, or SCF policy. Blocks may contain ordinary ORCA
settings, but cannot duplicate the generated resource or geometry directives.

The frequency policy can merely parse negative modes or require a minimum. A
configured tolerance is applied to the absolute magnitude of negative modes in
cm⁻¹. No molecule-specific threshold is a universal default. SP validation
checks successful calculation, SCF convergence, and final energy; the user—not
CMW—decides whether its method is higher-level.

## Overrides

Effective values use this precedence:

1. explicit workflow options such as `--nprocs 4`;
2. environment variables `ORCA_EXE`, `NPROCS`, `MAXCORE_MB`,
   `CMW_REQUIRE_MINIMUM`, and `CMW_IMAGINARY_TOLERANCE_CM1`;
3. JSON configuration;
4. documented defaults (`orca`, one process, 1000 MB per process, parse-only
   frequency policy, and zero tolerance).

The resolved values are included in the plan and attempt metadata. CMW rejects
reported resources that contradict the generated ORCA input.

## Resume, reuse, and attempts

Stage results are shared below the selected output root by exact stage target
identity. A later `opt+freq` workflow can reuse a valid OPT from `opt`; a later
`opt+freq+sp` workflow can reuse valid OPT and FREQ stages. Changing only SP
settings creates a new SP target without rerunning matching OPT/FREQ targets.

Reuse requires exact target identity, valid execution and science, and intact
required artifact sizes and hashes. Mismatches create a distinct attempt and
never overwrite the prior result. If an incomplete workflow must be tried again
with the same target, pass `--new-attempt`; previously valid prerequisite stages
remain reusable.

Each versioned workflow result records the mode and workflow target, input
identity, selected/skipped/reused/executed stages, validity, artifacts,
provenance references, exact geometry lineage, and failure point.

## Scope limits

This milestone does not implement FMO or ESP cubes, IGMH, Multiwfn,
interaction-energy decomposition, dimer generation or selection, catalytic
pathway energetics, VASP, pymatgen, ASE, SLURM, cluster locking, or generic
retry ladders.
