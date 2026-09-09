# Computational Modelling Workflow

A public, reusable, stage-aligned computational modelling architecture spanning
finite molecular systems and periodic materials systems. The project is intended
to turn proven research workflows into inspectable Python infrastructure without
erasing the scientific differences between molecular and periodic modelling.

## Current status

This repository currently contains:

- the existing static website that visualises the computational architecture;
- an installable `cmw` Python package with safe YAML loading for domain adapters;
- advisory process-tree health monitoring;
- strict XYZ parsing and stable geometry identity;
- ASE-based molecular XYZ-to-POSCAR conversion with deterministic species
  grouping, atom mapping, vacuum validation, and provenance;
- factual ORCA output parsing with separate OPT/FREQ/SP/TDDFT validation;
- versioned targets, attempts, artifacts, lineage, local locks, and fail-closed
  reuse;
- [ExecutionLayout v2](docs/execution-layout-v2.md), which keeps full scientific
  identities in authoritative manifests while paths use one short opaque target
  display layer, with transactional v1 migration and registry-backed resolution;
- deterministic, dry-run-first cleanup of provably superseded attempts with
  bounded provenance tombstones, explicit purge confirmation, and crash-safe
  recovery;
- a system-agnostic calculation/aggregation/derived-result workflow graph with
  typed scientific artifact contracts;
- optional method-aware ORCA protocol validation for method, basis, PNO, LED,
  fragment, SCF, optimization, and frequency evidence;
- ORCA 6.1.1 fixed-geometry intermolecular LED with exact ghost-basis fragment
  references, six-component numerical reconstruction, typed finalization, and
  hash-validated reuse;
- a HOF domain adapter that validates two-fragment hydrogen-bonded systems and
  translates their YAML configuration into generic CP/LED workflow contracts;
- shell-owned ORCA execution;
- a configurable, resumable `opt`, `opt+sp`, `opt+freq`, or `opt+freq+sp`
  molecular workflow with synthetic integration tests;
- a reusable Multiwfn 3.8.x runtime with immutable run-local settings,
  eight-thread default, executable provenance, and advisory process health;
- independent, source-aware HOMO/LUMO, density/ESP, and interfragment IGMH cube
  workflows with structural validation, grid compatibility, and fail-closed
  reuse, plus deterministic command/output contracts and typed
  density-to-IGMH artifact lineage; and
- a molecule-agnostic vertical stacking framework for explicit periodic
  registry extraction, deterministic dimer assembly, constrained/full
  relaxation planning, TDDFT/TDA and NTO artifacts, and Multiwfn-style
  hole/electron analysis.

It does **not** redistribute Multiwfn or contain dimer-selection science,
catalytic pathways, pymatgen, VASP execution, `INCAR`/`KPOINTS`/`POTCAR`
generation, scheduler execution, cluster locking, or generic retry ladders.

## Architectural scope

The molecular branch proceeds from chemical identity and finite structures
through shared structure modelling, validation, ORCA, and ORCA/Multiwfn
post-processing. The periodic branch proceeds from crystallographic evidence
through shared structure modelling, validation, VASP, and periodic analysis.
Both branches should converge on common provenance, validation concepts,
analysis, plotting, and reporting where that convergence is scientifically
meaningful.

Long-term development may extend the implemented molecular foundation toward
shared structure modelling, additional molecular analysis, periodic workflows,
and common reporting where scientifically justified. See
[`docs/architecture/README.md`](docs/architecture/README.md) for the target
architecture. The workflow website preserves the more detailed visual reference
and current method strategy.

The composite graph, typed artifact, compatibility, and protocol-validation
contracts are documented in
[`docs/architecture/composite-workflow-engine.md`](docs/architecture/composite-workflow-engine.md).
The reusable stacking contracts and artifact lineage are documented in
[`docs/architecture/vertical-stacking-workflow.md`](docs/architecture/vertical-stacking-workflow.md).
The HOF YAML mapping, validators, CP/LED graph, and non-executing ORCA input
plans are documented in [`docs/adapters/hof.md`](docs/adapters/hof.md).
The generic cleanup eligibility, plan-hash, transaction, registry, and purge
contracts are documented in
[`docs/architecture/attempt-cleanup.md`](docs/architecture/attempt-cleanup.md).

## Repository layout

```text
.
├── .github/
│   └── workflows/
│       └── pages.yml
├── docs/
│   ├── architecture/
│   ├── molecular/
│   ├── structure/
│   └── website/
├── examples/molecular/
│   ├── orca/
│   └── multiwfn/
├── scripts/
│   ├── orca/
│   └── workflows/
├── src/
│   └── cmw/
│       ├── adapters/
│       ├── core/
│       ├── molecular/
│       └── structure/
├── tests/
│   ├── fixtures/
│   ├── runtime/
│   └── workflows/
├── .gitignore
├── AGENTS.md
├── LICENSE
├── README.md
└── pyproject.toml
```

## Development philosophy

- Extract abstractions from demonstrated workflows instead of designing an
  empty framework in advance.
- Keep execution success separate from scientific validity.
- Treat provenance, methodological comparability, and reproducibility as
  first-class requirements.
- Align molecular and periodic stages where concepts are genuinely shared;
  preserve backend-specific behavior where they differ.
- Prefer deterministic, offline tests and scientifically meaningful validation
  over expensive routine calculations.

## Molecular workflow

The distribution is `computational-modelling-workflow`; its import namespace is
`cmw`. Version `0.1.0` remains a pre-alpha API. ASE provides structure I/O and
PyYAML is used with `safe_load` for explicit adapter configuration.
Inspect a public synthetic example without launching ORCA:

```sh
./examples/molecular/orca/opt_freq_sp/run.sh --plan
```

The four modes, JSON configuration, override precedence, validation policy,
geometry lineage, and resume behavior are documented in
[`docs/molecular/orca-opt-freq-sp.md`](docs/molecular/orca-opt-freq-sp.md).
Shell owns operational orchestration; Python owns scientific semantics and
structured state.

The generic three-calculation intermolecular LED contract, numerical equations,
ORCA 6.1.1 grammar boundary, and refinalization/reuse behavior are documented in
[`docs/molecular/orca-intermolecular-led.md`](docs/molecular/orca-intermolecular-led.md).

Molecular XYZ files can be converted to centered orthorhombic or cubic VASP
POSCAR cells with charge-aware automatic cell selection, per-face vacuum,
deterministic species grouping, read-back validation, atom-index mapping, and
conversion provenance. See
[`docs/structure/xyz-to-poscar.md`](docs/structure/xyz-to-poscar.md).

Validated ORCA results can be consumed independently by the downstream FMO,
ESP, and IGMH shell interfaces. Their source contract, explicit grid/fragment
configuration, runtime behavior, cube validation, and plan examples are
documented in
[`docs/molecular/multiwfn-cubes.md`](docs/molecular/multiwfn-cubes.md).

Prepared local commands can be executed one at a time through `cmw jobs`, with
a persistent queue, detached controller and optional terminal console. See
[`docs/cmw-jobs.md`](docs/cmw-jobs.md) for installation, controls, ownership
limits and a safe synthetic demo.

Development checks remain deterministic and require no ORCA installation:

```sh
python3 -m pip install -e .
python3 -m unittest discover -s tests
```

## Website

The static HTML/CSS/JavaScript source lives in `docs/website/` and has no build
step or external dependency. Preview it locally with:

```sh
python3 -m http.server --directory docs/website 8000
```

Then visit `http://localhost:8000`. GitHub Pages deployment is defined by
`.github/workflows/pages.yml`, which publishes `docs/website/` directly.

## License

This project is licensed under the MIT License. See `LICENSE` for details.

## Author

**Angze Li**

Computational chemistry and materials modelling.
