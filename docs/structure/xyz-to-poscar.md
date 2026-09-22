# Molecular XYZ to VASP POSCAR

`cmw structure embed-molecule` uses ASE and the existing CMW conversion algorithm
to prepare one finite molecular XYZ structure in a centered periodic vacuum cell.
This is a modeling transformation, not preservation of an existing periodic cell.
It accepts ordinary `.xyz` with Cartesian coordinates in ångström; CIF, `.extxyz`,
cell/PBC/property metadata in the XYZ comment, and additional per-atom columns
are rejected. Periodic import and occupancy/disorder handling are outside this
command's scope.

## Canonical command: POSCAR here, preparation record in Scratch

The installed `cmw` command works without a source checkout or scientific engine.
For a synthetic example, explicitly create separate input and Scratch parents:

```sh
mkdir -p example-inputs example-scratch
printf '3\nSynthetic molecule\nO 0 0 0\nH 0.96 0 0\nH -0.24 0.93 0\n' > molecule.xyz
cmw structure embed-molecule --input molecule.xyz --output example-inputs/POSCAR \
  --scratch-root example-scratch --record-directory water-embedding \
  --vacuum 10 --net-charge 0 --dry-run --json
cmw structure embed-molecule --input molecule.xyz --output example-inputs/POSCAR \
  --scratch-root example-scratch --record-directory water-embedding \
  --vacuum 10 --net-charge 0 --json
```

After successful preparation:

```text
example-inputs/POSCAR
example-scratch/water-embedding/preparation.json
```

No mapping or metadata sidecar is written beside POSCAR. This standalone command
creates only the requested POSCAR; it does not fabricate the other three VASP
inputs. Scratch `preparation.json` contains conversion metadata, inverse atom
mappings, requested/resolved settings and origins, source/prepared byte identities,
validation scope, and publication state. Observed effective inputs remain
`unobserved`: preparation does not show what a future runner actually uses.

Input/output/Scratch-root paths are relative to the current working directory.
A relative `--record-directory` is relative to `--scratch-root`. Both destination
parents must already exist; Scratch root must be an existing direct directory.
Use optional `--scratch-mount PATH` to require a particular mounted volume.
Paths through symlinks, overlapping source/destinations, existing output files,
and existing record directories are refused. There is no force/overwrite option.
Missing Scratch is also a blocker during preview, which creates no directory,
record, lock, journal, mapping, or other file.

The two destinations are not one atomic transaction. An exclusively owned
Scratch record stores intent before same-filesystem staging of POSCAR, and a
no-clobber link publishes the file. Success requires a finalized matching Scratch
record. If record finalization fails after POSCAR publication, the command returns
an error and retains both POSCAR and an incomplete diagnostic record; it does not delete or
overwrite the published file. Read-only inspection is available through
`cmw.core.preparation_publication.inspect_publication(record_path)`. It reports
incomplete publication or mismatched bytes without repairing either location.
CLI success exits 0; invalid input or publication failure exits 2 with a stable
`code` and reason in JSON. These decisions cover embedding/publication, not
scientific adequacy or convergence.

See [preparation publication](../periodic/preparation-publication.md) for the
shared no-clobber protocol, incomplete states, and durability limits.

## Legacy interface

The shell script, `python -m cmw.structure.cli`, and
`cmw.structure.xyz_to_poscar` retain their existing overwrite and adjacent-sidecar
behavior. They use the same coordinate/mapping algorithm and POSCAR renderer as
the canonical command:

```text
XYZ -> ASE -> centered vacuum cell -> species regrouping -> POSCAR
                                                       -> atom_mapping.json
                                                       -> conversion_metadata.json
```

VASP remains periodic in all three directions. The vacuum cell only
approximates an isolated molecule; it does not eliminate periodic electrostatic
interactions. For a typical molecular starting point, use approximately
10–12 Å between the outermost atom and **each** cell face.

## Charge-aware cell selection

`--cell-mode auto` is the default. Its deterministic rule is:

```text
neutral isolated molecule
-> --net-charge 0
-> auto
-> orthorhombic cell

charged isolated molecule
-> --net-charge +/-N
-> auto
-> cubic cell
```

`net_charge` is the formal total molecular charge supplied explicitly by the
caller. CMW does not infer charge, electron count, or oxidation states from the
XYZ composition. Changing the default request from `orthorhombic` to `auto`
does not change the generated cell for a neutral molecule; existing explicit
`orthorhombic` and `cubic` selections retain their behavior.

After `uv sync`, run from the repository root (omit `PYTHON_BIN` if the
environment is already activated):

```sh
PYTHON_BIN=.venv/bin/python ./scripts/workflows/xyz_to_poscar.sh molecule.xyz \
  --output POSCAR \
  --vacuum 10 \
  --net-charge 0
```

The resolved orthorhombic mode uses each molecular span plus twice the requested
vacuum. Cubic mode uses the largest molecular span for all three cell lengths.
`--dry-run` validates the XYZ and reports the net charge, requested and resolved
cell modes, selection reason, warnings, and proposed cell without writing files.

For example, a user-declared +2 Cu complex is prepared in a cubic cell without
requiring an explicit cell override:

```sh
PYTHON_BIN=.venv/bin/python ./scripts/workflows/xyz_to_poscar.sh cu_complex.xyz \
  --output POSCAR \
  --vacuum 12 \
  --net-charge 2
```

An explicit charged `--cell-mode orthorhombic` remains allowed, but normal and
dry-run reports warn that non-cubic cells can be problematic for some VASP
isolated-charge electrostatic correction workflows. The warning also records
that the caller overrode CMW's recommended automatic cubic choice.

VASP species counts describe contiguous coordinate groups. CMW therefore
groups atoms by element while preserving first-occurrence species order and the
original relative order within each group. Override the complete order when
needed:

```sh
PYTHON_BIN=.venv/bin/python ./scripts/workflows/xyz_to_poscar.sh copper-complex.xyz \
  --output POSCAR \
  --vacuum 12 \
  --species-order C H N O Cu
```

An explicit order must contain every element exactly once. The accompanying
`atom_mapping.json` records both directions using zero-based CMW indices and
one-based human atom numbers. `conversion_metadata.json` records file hashes,
composition, species order, cell vectors, actual six-face clearances, ASE
version, and read-back validation. No timestamp is included, so identical
inputs and options regenerate byte-identical scientific outputs in the same
locations.

The Python API is:

```python
from cmw.structure import xyz_to_poscar

metadata = xyz_to_poscar(
    "molecule.xyz",
    "POSCAR",
    vacuum=12.0,
    cell_mode="auto",
    net_charge=2,
)
```

`conversion_metadata.json` distinguishes `requested_cell_mode` from
`resolved_cell_mode` and records `net_charge`, `cell_selection_reason`, and any
warnings. The legacy `cell_mode` field remains as an alias for the resolved
mode. Metadata stays deterministic and contains no timestamp.

Cubic selection is structural preparation only. This utility does not create
or enable `LDIPOL`, `IDIPOL`, `NELECT`, `MAGMOM`, or other `INCAR` settings; it
does not create `KPOINTS` or `POTCAR`, infer oxidation states, or launch VASP.
