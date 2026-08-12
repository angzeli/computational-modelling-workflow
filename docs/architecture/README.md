# Computational architecture

This document describes the architecture the repository implements
progressively. The first reusable molecular foundation now exists; other
branches in the diagrams remain targets rather than implementation claims.

> The website describes the broader computational architecture; the Python and
> shell layers implement only the explicitly documented reusable milestones.

## Implemented molecular stack

The current package implements a dependency-free OPT / optional FREQ / optional
SP workflow for ORCA. Shell owns executable discovery, environment handling,
process launch, descriptors, signals, temporary directories, traps, cleanup,
and sequential calls into Python. Python owns XYZ and ORCA parsing, validation,
scientific targets, execution attempts, provenance, artifacts, geometry
lineage, locks, reuse decisions, plans, and structured results.

The validated ORCA result is also an explicit downstream source contract. A
shell-owned Multiwfn runtime launches three independent analyses while Python
owns source identity, operation semantics, cube parsing, paired-grid checks,
fragment validation, target/attempt provenance, and reuse:

```text
validated ORCA result
        │
        ▼
compatible recorded wavefunction
        │
        ├──────────────┬─────────────────┐
        ▼              ▼                 ▼
  HOMO/LUMO          density + ESP      IGMH
     cubes             cubes            cubes
        └──────────────┴─────────────────┘
                       │
                       ▼
          validated provenance-rich artifacts
```

IGMH additionally requires an explicit two-fragment complete partition and an
explicit grid configuration. These analyses are downstream siblings, not new
`opt+freq+sp` mode combinations.

Process health is operational evidence only and cannot certify scientific
completion. ORCA execution and scientific status remain separate. Target
identity represents scientific intent; attempt identity represents the
resources and executable used for one realization. Reuse fails closed unless
target identity, scientific validity, and artifact integrity all agree.

See the focused boundary documents in this directory and the
[workflow guide](../molecular/orca-opt-freq-sp.md).

## Domain branches

Finite and molecular systems follow this target path:

```text
finite / molecular systems
        │
        ├── structural definition
        └── shared structure modelling
                     │
                     ▼
                    ASE
                     │
              structure validation
                     │
                     ▼
                   ORCA
                     │
       validated ORCA result + Multiwfn
                     │
                     ▼
         validated molecular result ──┐
                                      │
                                      ▼
                            common Python analysis
                                      │
                                      ▼
                             scientific outputs
```

Periodic and materials systems follow a distinct target path:

```text
periodic / materials systems
        │
        ├── crystallographic evidence
        └── shared structure modelling
                     │
                ASE + pymatgen
                     │
              structure validation
                     │
                     ▼
                   VASP
                     │
          periodic analysis stack
                     │
                     ▼
          validated periodic result ──┘
                                      │
                                      ▼
                            common Python analysis
                                      │
                                      ▼
                             scientific outputs
```

The branches should share configuration, environment handling, execution,
provenance, status, monitoring, validation, resource management, testing,
automated QA, data management, and reproducibility concepts where appropriate.
A process that exits successfully is not necessarily scientifically valid, and
results are comparable only when their methods and provenance support the
comparison.

## Package decomposition

The implemented core, structure, ORCA, Multiwfn runtime, and downstream
workflow directories establish the molecular decomposition. Periodic and
general analysis layers remain future scope:

```text
cmw
├── core
├── structure
├── molecular
│   ├── orca
│   └── multiwfn
├── periodic
│   ├── vasp
│   └── pymatgen
└── analysis
```

These are conceptual boundaries, not directories to create pre-emptively. A
module should be introduced only when a demonstrated workflow supplies a real,
reusable implementation and a meaningful behavioral contract.

Molecular and periodic workflows should converge on shared validation,
provenance, status, and analysis concepts where scientifically justified. They
should not be forced into artificial symmetry: ORCA and VASP have different
inputs, execution models, outputs, and domain-specific validation requirements.

Reusable code belongs under `src/cmw/`; the website belongs under
`docs/website/`; and architecture documentation belongs under
`docs/architecture/`. Molecule- or material-specific assumptions, private
research data, machine-specific paths, licensed software, and unpublished
results remain outside this public package unless they are demonstrably generic
and legally distributable.
