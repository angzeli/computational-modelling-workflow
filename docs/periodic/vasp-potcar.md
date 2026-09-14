# Local VASP POTCAR preparation

CMW reads your separately installed, licensed POTPAW library. It supplies no
potentials and downloads nothing. Normal use creates only the requested POTCAR;
no YAML, project configuration, or metadata sidecar is required.

```sh
export CMW_VASP_POTCAR_ROOT=/path/to/potpaw_PBE
cmw vasp potcar build
cmw vasp potcar check
cmw vasp potcar list In
cmw vasp potcar build --pot In=In_d --force
```

Root precedence is explicit `--potcar-root PATH`, then
`CMW_VASP_POTCAR_ROOT`, otherwise an error explaining both options. These paths
are supplied by the caller; CMW has no built-in library location.

The default is always the suffix-free directory matching each explicit POSCAR
species: `Zn In S` selects `Zn/POTCAR`, `In/POTCAR`, `S/POTCAR`, in that exact
order. A missing base potential fails even if suffixed variants exist. CMW does
not choose a scientifically preferred variant.

Repeat `--pot ELEMENT=VARIANT` to override individual elements for one invocation:

```sh
cmw vasp potcar build /path/to/POSCAR --output /path/to/POTCAR \
  --potcar-root /path/to/potpaw_PBE --pot In=In_d --pot Zn=Zn_sv
cmw vasp potcar check /path/to/POSCAR --potcar /path/to/POTCAR
```

Use `list` to discover which variants actually exist before requesting them.
Overrides map by element, independently of argument order. Absent elements,
conflicting duplicates, unsafe paths, and mismatched dataset elements fail.
Repeated identical overrides are accepted. Repeated species blocks in a POSCAR
remain repeated and receive the same element override.

Build validates every selected input before writing, writes a private temporary
file beside the destination, validates the assembled dataset count and species
order, then atomically publishes it. Existing outputs require `--force`;
publication without force also refuses an output created by a concurrent writer.
Failures clean up temporary files and preserve an existing destination. The output
parent directory must exist. Overwriting the POSCAR or a file inside the selected
library is rejected even with force. Potential directory/file symlinks must stay
within the library root.

POSCAR parsing requires explicit VASP 5/6 element symbols and matching counts;
VASP 4 identities and annotated/ambiguous symbols are never guessed. This is a
species-header reader, not full geometry validation. POTCAR checking requires
terminated datasets with recognizable `TITEL` and/or `VRHFIN` metadata; when both
exist, their element identities must agree. A variant such as `In_d` is chemically
In and passes against an In species block. Validation establishes metadata identity
and boundaries, not numerical dataset integrity or scientific suitability.

`list ELEMENT` considers base, underscore-suffixed and dot-suffixed directories,
validates their metadata, and reports their names without proprietary contents.
Unrelated prefixes such as Ca and Cl are excluded when listing C. A malformed
matching library entry is reported as an error so a damaged library is visible.

The reusable implementation is `cmw.periodic.vasp.potentials`; argparse presentation
is in `cmw.periodic.vasp.cli`, registered by the canonical `cmw` entry point.
Tests use entirely synthetic metadata in temporary directories. Never add real
POTCAR files to the repository, including outputs with custom filenames.
