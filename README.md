# Computational Modelling Workflow

A public, reusable, stage-aligned computational modelling architecture spanning
finite molecular systems and periodic materials systems. The project is intended
to turn proven research workflows into inspectable Python infrastructure without
erasing the scientific differences between molecular and periodic modelling.

## Current status

This repository currently contains:

- the existing static website that visualises the computational architecture;
- a concise description of the target architecture and its boundaries;
- a minimal installable `cmw` Python package shell; and
- development guidance for later extraction of reusable implementations.

It does **not yet contain** reusable ORCA runtime code, Multiwfn workflows, VASP
workflows, pymatgen integration, or a complete shared validation and provenance
layer. The package is deliberately small until real reusable code is ready to be
extracted and tested.

## Architectural scope

The molecular branch proceeds from chemical identity and finite structures
through shared structure modelling, validation, ORCA, and ORCA/Multiwfn
post-processing. The periodic branch proceeds from crystallographic evidence
through shared structure modelling, validation, VASP, and periodic analysis.
Both branches should converge on common provenance, validation concepts,
analysis, plotting, and reporting where that convergence is scientifically
meaningful.

Long-term development is intended to cover shared structure modelling with ASE,
ORCA, Multiwfn, VASP, pymatgen, scientific validation, provenance, execution and
resource management, and common analysis and reporting. See
[`docs/architecture/README.md`](docs/architecture/README.md) for the target
architecture. The workflow website preserves the more detailed visual reference
and current method strategy.

## Repository layout

```text
.
├── .github/
│   └── workflows/
│       └── pages.yml
├── docs/
│   ├── architecture/
│   │   └── README.md
│   └── website/
│       ├── index.html
│       └── assets/
│           ├── css/workflow.css
│           └── js/workflow.js
├── src/
│   └── cmw/
│       └── __init__.py
├── tests/
│   └── test_package.py
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

## Python package shell

The distribution is `computational-modelling-workflow`; its import namespace is
`cmw`. Version `0.1.0` marks an initial pre-alpha development API. It has no
runtime dependencies and no command-line interface.

```sh
python3 -m pip install -e .
python3 -c "import cmw"
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
