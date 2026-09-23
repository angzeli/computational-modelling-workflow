# Ordered CIF to POSCAR

`cmw structure import-cif` imports one ordered, fully occupied periodic structure
from a core CIF 1.1 block. Gemmi (`>=0.7.5,<0.8`) supplies crystallographic parsing
and symmetry data; CMW validates the supported contract and records the expansion.
ASE writes the resulting POSCAR. No VASP, ORCA, MPI or other scientific engine is
needed or launched, and the command does not create Jobs state.

The dependency choice is narrow: Gemmi's low-level CIF document/loop access keeps
source tokens available, and its operation and space-group APIs provide symmetry
primitives. CMW owns block selection, supported-model rejection, explicit orbit
expansion and provenance rather than treating successful parsing as model
acceptance. ASE remains the existing POSCAR writer; no second materials-modeling
framework is introduced. See Gemmi's
[CIF parser documentation](https://gemmi.readthedocs.io/en/latest/cif.html) and
[symmetry API guide](https://gemmi.readthedocs.io/en/latest/symmetry.html).

## Installed command and output

Run this synthetic example in a new working directory. The coordinates are
invented test data, not a recommended physical model:

```sh
mkdir inputs scratch
cat > ordered.cif <<'EOF'
data_synthetic
_cell_length_a 6
_cell_length_b 7
_cell_length_c 8
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
_space_group_name_H-M_alt 'P -1'
_space_group_IT_number 2
loop_
_space_group_symop_id
_space_group_symop_operation_xyz
1 'x,y,z'
2 '-x,-y,-z'
loop_
_atom_site_label
_atom_site_type_symbol
_atom_site_fract_x
_atom_site_fract_y
_atom_site_fract_z
_atom_site_occupancy
H1 H 0.1 0.2 0.3 1
EOF
cmw structure import-cif --input ordered.cif --output inputs/POSCAR \
  --scratch-root scratch --record-directory imported --block synthetic \
  --dry-run --json
cmw structure import-cif --input ordered.cif --output inputs/POSCAR \
  --scratch-root scratch --record-directory imported --block synthetic --json
```

Successful materialization creates only:

```text
inputs/POSCAR
scratch/imported/preparation.json
```

The import record has `record_kind: periodic-structure-import`. It contains the
source-byte identity, selected block, parser/version evidence, represented cell,
raw source-site information, symmetry operations and expansion provenance,
output ordering and exact prepared-byte identity. It does not copy CIF or POSCAR
payloads into Scratch. There are no sidecars beside POSCAR.

Input, output and Scratch-root paths resolve from cwd. A relative record directory
resolves beneath the explicit Scratch root. Both destination parents and Scratch
root must already exist; the output file and record directory must be new. Source
overlap, symlink traversal and overwrite are refused. Preview performs parsing,
expansion, rendering and validation in memory, including destination checks, and
creates no directories, files, records or locks. The molecular embedding command
remains separate and continues to reject CIF and periodic extended XYZ.

Success exits 0. Unsupported or malformed input and publication failures exit 2
with structured JSON diagnostics. These checks establish supported import and
publication, not scientific suitability. Input publication and Scratch record
completion are distinct phases: an error after POSCAR publication retains it and
reports incomplete preparation. See the
[shared publication contract](../periodic/preparation-publication.md) for failure
inspection, no-clobber behavior and durability limits.

## Cell, symmetry and ordering

The importer retains the supplied cell lengths, angles and crystallographic basis.
Its Cartesian representation places `a` along positive x, `b` in the xy plane,
and the positive-volume `c` component along positive z. This fixes a Cartesian
orientation for CIF cell metrics; it does not choose a primitive cell, conventional
cell, alternative basis, new lattice or common interface frame. No cell reduction,
vacuum addition, molecular centering or strain is applied.

One selected block must provide explicit complete symmetry operations or an
unambiguous space-group setting. Files with multiple structural blocks require
explicit `--block`; one structural block can be selected automatically alongside
metadata-only blocks. The command never chooses a structure by atom count or
other heuristic. Ambiguous settings, conflicting declarations, incomplete operations and unsupported CIF
dialects are rejected. Partial occupancy and unresolved disorder are rejected;
the importer never selects a majority element, rounds occupancy or deletes a site
to make an ordered model.

An absent occupancy column uses the coreCIF dictionary default of `1.0`, with that
basis recorded for every source site. An explicitly supplied `?`, `.`, nonfinite
value or value other than `1.0` is rejected; CMW does not replace an explicit
unknown with the dictionary default. This is the importer's conservative handling
of the [IUCr occupancy definition](https://www.iucr.org/__data/iucr/cifdic_html/1/cif_core.dic/Iatom_site_occupancy.html).
Nonzero or unknown `_atom_site_attached_hydrogens` is also rejected: these are
hydrogens without explicit coordinates, so accepting them would leave the model
incomplete. Explicit zero is accepted. See the
[IUCr attached-hydrogen definition](https://www.iucr.org/__data/iucr/cifdic_html/1/cif_core.dic/Iatom_site_attached_hydrogens.html).

Symmetry expansion follows source-site order and a canonical operation order:
identity first, followed by lexically sorted operation triplets.
Fractional coordinates use modulo-one representatives rounded to 12 decimal
places. Special positions collapse repeated images from the same source-site
orbit only, retaining every generating operation ID. Periodic fractional
coordinate comparisons use an absolute `1e-8` tolerance; cases merely near a
special position by more than `1e-12` are ambiguous and rejected instead of
silently snapping a site. Overlap between distinct source sites is rejected,
including chemically identical sites. Tolerances are fractional-coordinate
rules, not general Cartesian overlap radii or chemical bonding criteria.
Source-to-wrapped and symmetry-image-to-wrapped coordinates are additionally
checked modulo integer lattice translations: their residual Cartesian distance
must be at most `1e-8` ångström. A rounding step that exceeds this bound is rejected.

The source rows retain site index/identifier, raw label, type symbol, fractional
coordinates and occupancy. One source site may generate several expanded sites.
That relationship is recorded separately from the bijection between expanded
sites and output POSCAR atoms. Stable species grouping follows first occurrence
and preserves order within each species. It does not erase the original row or
operation provenance. Per-site scientific arrays are not inferred from labels.

The [immutable periodic model](periodic-model.md) exposes the represented cell,
ordered species, fractional and Cartesian coordinates, source/expanded indices,
operation IDs, source SHA-256 and selected block. Its `structure_id` describes
ordered geometry at documented precision, separately from the source-byte hash;
it does not claim general crystallographic or scientific equivalence.

## Downstream VASP preparation

An imported POSCAR is an explicit source for `cmw vasp prepare`. In an otherwise
complete caller-owned preparation specification, set these fields:

```json
{
  "structure": "inputs/POSCAR",
  "source_record": "scratch/imported/preparation.json"
}
```

These paths resolve from the specification's directory. The preparation adapter
requires a completed matching import record and exactly matching POSCAR bytes,
and classifies its source provenance as `periodic_structure_import`. It preserves
the imported POSCAR bytes. The full calculation directory still contains exactly
INCAR, KPOINTS, POSCAR and POTCAR; its preparation record stays in a separately
selected Scratch directory. Supply the scientific profile, potential selection
and other required context described in the
[VASP preparation guide](../periodic/vasp-preparation.md). Import alone supplies
none of those physical-model choices.

## Future interface-construction handoff

Two accepted imports provide **periodic structure A + periodic structure B** to
a future explicit interface-construction operation. Combining two CIFs is not
simple concatenation: their cells, surfaces, relative geometry and boundary
conditions require explicit decisions. This is a documented handoff,
not an implemented command, automatic matching algorithm or default recipe.
The operation would require caller decisions for all of the following:

| Choice | Required scientific declaration |
|---|---|
| Input state | Whether each input is bulk or already a slab |
| Surfaces | Surface orientation/Miller plane for each bulk input |
| Slabs | Termination and thickness for each slab |
| In-plane match | Which in-plane lattices and integer supercells to match |
| Relative orientation | In-plane rotation or twist between A and B |
| Mismatch | Accepted mismatch tolerance and how it is measured |
| Strain | Strain A, strain B, split strain, or reject the match |
| Registry | Relative stacking and in-plane translation |
| Separation | Initial distance between the contacting surfaces |
| Overlap | How to treat overlapping atoms, with explicit acceptance criteria |
| Boundary geometry | Vacuum-containing slab or fully periodic interface |
| Interfaces | Number and identity of interfaces created by periodicity |
| Atom lineage | A/B origin and input-atom mapping for every output atom |

There are no implicit selections for these choices. Import does not build slabs,
choose facets, match lattices, make supercells, twist, strain, stack, remove atoms,
add vacuum or construct interfaces. The periodic result retains enough geometry
and lineage for a later operation to accept explicit choices and preserve the
origin of every atom.
