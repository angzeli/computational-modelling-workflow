# Computational Modelling Workflow

A static, visual reference for a personal computational chemistry and materials
modelling architecture. The site follows the full research path from structure
definition and reproducible model generation through validation, quantum or
periodic calculation, scientific analysis, and publication-ready output.

The workflow separates two related modelling branches:

- **Finite molecular systems** use a common reference structure, ASE for
  systematic model construction, and ORCA as the electronic-structure backend.
- **Periodic materials systems** use ASE and pymatgen for crystal, slab, defect,
  and interface models, with VASP as the periodic DFT backend.

Both branches converge on Python for parsing, automated quality checks,
comparison, plotting, and reproducible report generation. Git, Linux, remote
execution, schedulers, provenance, testing, and data management support every
layer.

## Open locally

The site has no build step or external dependencies. Open `index.html` directly
in a browser, or serve the repository folder with any small static file server.
For example:

```sh
python3 -m http.server 8000
```

Then open `http://localhost:8000`.

## Deploy with GitHub Pages

1. Push the repository to GitHub.
2. Open **Settings → Pages** in the repository.
3. Under **Build and deployment**, choose **Deploy from a branch**.
4. Select the `main` branch and the repository root (`/`), then save.

GitHub Pages can publish the root `index.html` directly; no site generator or
build workflow is required.

This architecture is intentionally a living reference and should evolve as new
methods, checks, and tools are adopted.
