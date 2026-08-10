# Computational Modelling Workflow

A stage-aligned visual reference for computational chemistry and materials
modelling, covering finite molecular systems and periodic extended systems from
structure preparation to validated scientific output. It is a living personal
computational architecture, not a quantum-chemistry package or workflow engine.

## Overview

The workflow distinguishes two modelling domains without equating them with
organic and inorganic chemistry:

- **Finite / molecular systems:** molecules, dimers, clusters, and finite
  fragments.
- **Periodic / extended systems:** crystals, COFs, MOFs, surfaces, defects, and
  interfaces.

Both branches meet in a shared structure-modelling layer centred on ASE before
proceeding to validation and their respective electronic-structure backends.

## Workflow architecture

**Finite / molecular**

`ChemDraw → Avogadro 2 → ASE → structure validation → ORCA → ORCA parsers / Multiwfn → validated finite result`

**Periodic / extended**

`CIF / structure source → VESTA → ASE + pymatgen → structure validation → VASP → pymatgen / VASPKIT / sumo / Bader / VESTA → validated periodic result`

**Shared downstream layer**

`Validated results → Python (NumPy, pandas, matplotlib) → scientific output`

The HTML site is the detailed visual reference; this README provides only the
architectural outline.

## Structure-modelling philosophy

ASE acts as the shared structural abstraction layer, supporting reproducible
generation and manipulation of atomic structures before they are passed to
different calculation backends. Finite models typically proceed to ORCA, while
periodic models typically proceed to VASP. On the periodic side, pymatgen
complements ASE with crystallography, symmetry, structure standardisation, and
VASP-oriented workflows.

## Method strategy

The architecture is method-aware rather than fixed to one level of theory. The
current finite-system strategy includes r²SCAN-3c for efficient screening and
geometry exploration, ωB97X-D4 / ma-def2-TZVP with RIJCOSX acceleration for
production DFT, and selected DLPNO-CCSD(T) benchmark single points where
justified. These choices describe the present personal workflow and may evolve.

## Repository structure

```text
.
├── .gitignore
├── index.html
├── assets/
│   ├── css/
│   │   └── workflow.css
│   └── js/
│       └── workflow.js
├── README.md
└── LICENSE
```

## View locally

The site is static and has no build step or external dependencies. Serve the
repository directory with:

```sh
python3 -m http.server 8000
```

Then visit `http://localhost:8000`. Opening `index.html` directly in a browser
also works.

## GitHub Pages

GitHub Pages can serve the root `index.html` directly. Configure Pages to deploy
from the `main` branch and the repository root (`/`); no site generator or build
workflow is required.

## Design principles

- Vertical, stage-aligned workflow
- Clear finite-versus-periodic model distinction
- Shared structure abstraction before backend-specific calculation
- Explicit validation before scientific interpretation
- Calculation backends separated from post-processing and analysis
- Reproducible downstream analysis in Python
- Restrained day/night visual design

## License

This project is licensed under the MIT License. See `LICENSE` for details.

## Author

**Angze Li**

Computational chemistry and materials modelling.
