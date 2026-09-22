# Explicit VASP input preparation

The installed `cmw vasp prepare` command prepares fixed-geometry static inputs or
fixed-cell ionic relaxation inputs. It preserves an accepted source POSCAR byte
for byte and reuses CMW's local potential selector and read-only input checker.
It does not execute a scientific engine, submit a Job, infer convergence, or
certify a physical model. No molecular multiplicity or invented Job ID is needed.

## Output and paths

```text
project/inputs/case/             scratch/preparations/case/
  INCAR                           preparation.json
  KPOINTS
  POSCAR
  POTCAR
```

The input directory contains exactly those four regular files. There are no
metadata sidecars, links to Scratch, markers, copied specifications, README,
run scripts, or Git changes. The separately selected Scratch directory contains
only `preparation.json`. The command never stages generated inputs in Git.
Keep real POTCARs excluded through your project-level policy; CMW does not create
a fifth `.gitignore` in the input directory or distribute licensed potentials.

`--output`, `--scratch-root`, `--scratch-mount` and `--spec` resolve from cwd.
Source structure, baseline and source-record paths in a specification resolve
from that specification's directory. A separate profile JSON's potential root
resolves from the profile's directory; a case override of that root resolves
from the specification's directory. A relative `--record-directory` resolves
under `--scratch-root`. Source locations remain stable when cwd changes.

Scratch root and both destination parents must already exist. The new output
and record directories must not exist, even if empty. Symlink traversal and
source/destination overlap are refused. `--scratch-mount PATH` can require an
actual mounted filesystem containing Scratch. On macOS a Scratch root under
`/Volumes` must be on its mounted volume. Missing Scratch is an error, including
during preview; there is no fallback or force option.

## Caller-owned JSON specification

Profiles are JSON data with a required name, not executable hooks. JSON duplicate
keys and case-insensitive duplicate INCAR tags are errors. There are no global
scientific defaults. This specification uses entirely **synthetic test data**;
its numbers are not recommendations for a real calculation:

```json
{
  "schema_version": 1,
  "calculation": "static",
  "structure": "source.POSCAR",
  "profile": {
    "name": "synthetic-demonstration",
    "incar": {
      "GGA": "PE", "ENCUT": 300, "PREC": "Normal",
      "ISPIN": 1, "ISMEAR": 0, "SIGMA": 0.05,
      "EDIFF": 0.00001, "NELM": 20, "ALGO": "Normal",
      "ISTART": 0, "ICHARG": 2
    },
    "kpoints": {"mode": "Gamma", "mesh": [1, 1, 1], "shift": [0, 0, 0]},
    "potentials": {
      "root": "synthetic-library",
      "variants": {"H": "H"},
      "requirements": {"family": "INVENTED"}
    }
  },
  "overrides": {"incar": {"NELM": 24}},
  "runtime": {"mpi_ranks": 2, "threads_per_rank": 1, "cpus": 2,
              "incar": {"NCORE": 1, "KPAR": 1}}
}
```

`profile` may instead be a JSON file path containing the same named profile
object. `overrides` may replace individual INCAR keys or k-point fields and
potential fields. A replaced `variants` or `requirements` object replaces the
whole object, avoiding hidden nested merging. Every resolved field records its
named-profile, explicit-override, type-invariant or runtime origin.

Required INCAR context is GGA, ENCUT, PREC, ISPIN, ISMEAR, SIGMA, EDIFF, NELM,
ALGO, ISTART and ICHARG. The current preparation subset accepts GGA
PE/PS/RP/RE/AM/CA/PZ, PREC Normal/Accurate, ALGO Normal/Fast/VeryFast and
ISMEAR -5/-4/-1/0/1/2. These are explicit supported choices, not defaults or a
claim that every combination suits a system. Numerical positivity and relevant
array lengths are checked. Unknown tags are refused by preparation; the generic
checker retains them as unassessed. The full typed tag set is in
`cmw.periodic.vasp.inputs.KNOWN_TAGS`, with these further preparation restrictions:

- Fresh initialization must be explicit `ISTART=0`, `ICHARG=2`. Restart artifacts
  and frozen-density modes are unsupported.
- Static type supplies `NSW=0`, `IBRION=-1` only when absent. Conflicting supplied
  values are errors.
- `fixed-cell-relaxation` supplies `ISIF=2` only when absent and requires explicit
  `IBRION` 1/2/3, positive `NSW`, positive `POTIM`, and nonzero `EDIFFG`.
  Negative EDIFFG means a force criterion; positive means an energy criterion.
  The sign is preserved and recorded, never converted.
- Collinear ISPIN=2 requires explicit atom-ordered MAGMOM. Enabled DFT+U requires
  LDAUTYPE 1/2 and all three block-ordered LDAUL/LDAUU/LDAUJ arrays. Enabled LDIPOL
  requires explicit IDIPOL and DIPOL. Optional NELECT must be positive.
- Hybrid, meta-GGA, SOC/noncollinear, nonlocal-vdW, hybrid-specific controls and
  alternate IALGO selection are outside this MVP. CIF, periodic extxyz import,
  surface construction and scientific-result acceptance are also deferred.

All k-point fields are explicit: mode `Gamma` or `Monkhorst-Pack`, three positive
integer subdivisions, and three finite shifts in the regular-mesh convention.
No universal slab axis or N3=1 is imposed. Line mode, explicit point lists and
KPOINTS_OPT are unsupported. See the VASP definitions for
[POSCAR scaling and selective flags](https://vasp.at/wiki/index.php/POSCAR),
[regular k-point meshes](https://vasp.at/wiki/index.php/KPOINTS), and
[EDIFFG stopping conventions](https://vasp.at/wiki/index.php/EDIFFG).

The potential root, a variant for every distinct declared element, and an
explicit required family must be supplied. No real potential data are bundled.
Selection preserves every POSCAR block including repeated species. Parsed
variant/family evidence is mandatory; requested unavailable or mismatched
identity fails. A dataset date is not a library release. Additional identity
requirements and caller-declared `library_release` follow the
[POTCAR guide](vasp-potcar.md); an unrequested release remains unestablished.

## Commands and an executable synthetic setup

Run this only in a new, disposable demonstration directory. The invented potential
metadata is enough for parser tests and is not a usable VASP potential:

```sh
mkdir synthetic-library synthetic-library/H project scratch
cat > source.POSCAR <<'EOF'
Synthetic H
1
8 0 0
0 8 0
0 0 8
H
1
Cartesian
1 2 3
EOF
cat > synthetic-library/H/POTCAR <<'EOF'
TITEL = INVENTED H 01Jan2001
VRHFIN = H: invented metadata only
End of Dataset
EOF
```

Save the JSON above as `static.json`, then use the installed commands:

```sh
cmw vasp potcar build source.POSCAR --potcar-root synthetic-library --pot H=H \
  --output standalone.POTCAR --strict-identity --dry-run --json
cmw vasp potcar build source.POSCAR --potcar-root synthetic-library --pot H=H \
  --output standalone.POTCAR --strict-identity --json
cmw vasp potcar check source.POSCAR --potcar standalone.POTCAR --strict-identity --json
cmw vasp prepare --spec static.json --output project/static \
  --scratch-root scratch --record-directory static --dry-run --json
cmw vasp prepare --spec static.json --output project/static \
  --scratch-root scratch --record-directory static --json
cmw vasp check-inputs project/static --json
cmw vasp check-inputs project/static --preparation-record scratch/static/preparation.json --json
```

For fixed-cell relaxation, write a separate specification `relax.json` with
`"calculation": "fixed-cell-relaxation"` and explicitly add `IBRION: 2`,
`NSW: 3`, `POTIM: 0.5`, `EDIFFG: -0.05` to its profile INCAR. Then:

```sh
cmw vasp prepare --spec relax.json --output project/relax \
  --scratch-root scratch --record-directory relax --dry-run --json
cmw vasp prepare --spec relax.json --output project/relax \
  --scratch-root scratch --record-directory relax --json
```

Preview resolves actual selected bytes, validates all candidates and all
destination prerequisites, and returns a plan without writing directories,
records, journals, locks or sidecars. A later materialization reads and checks
the sources again; a preview does not freeze or authorize changed content.
Success is exit 0. Preparation rejection or incomplete publication is exit 2,
with structured JSON `code`, `message`, and relevant findings/record path.
Checker exits are separately defined as 0 valid, 1 invalid, 2 unsupported in the
[checker guide](vasp-input-checking.md).

## Source, prepared and effective evidence

The Scratch record captures source roles, recorded/resolved locations, exact
source and prepared byte identities, full resolved settings/origins, structure
and constraint summary, selected non-payload potential identifiers, validation
scope/findings, optional differences, renderer version and publication state.
Source POSCAR bytes and atom order are preserved. There is no regrouping,
wrapping, centering, vacuum adjustment or constraint removal in this command.

`preparation_content_id` identifies exact prepared content plus calculation type
and declared runtime; timestamps and filesystem locations are excluded. It is
not a scientific-equivalence identity: formatting changes can change it.
`scientific_target_id` remains null because this MVP does not establish a full
periodic scientific lifecycle target. Each materialization has its own unique
`preparation_id` and timestamp.

Runtime declarations stay separate from rendered scientific INCAR. Only positive
NCORE/KPAR are accepted as expected runtime INCAR changes. They are **not** applied
by preparation. MPI ranks, threads per rank and requested CPUs are distinct
declarations; basic product/divisibility checks apply when enough evidence is
supplied. No resource admission or observed utilization is inferred. A future
runner must separately implement its declared overlay. `effective_inputs` remains
`unobserved` with no invented execution evidence.

## Baseline comparison and embedding lineage

An optional specification field `"baseline": "project/static"` compares a bundle.
Or use `"baseline": {"record": "scratch/static/preparation.json"}` to include a
compatible record's runtime declaration and exact publication evidence. Write the
new specification outside input directories and select fresh output/record names.
The preparation JSON/stdout contains the comparison; no diff sidecar is created.

Comparison distinguishes scientific INCAR, geometry/cell, ordered species blocks,
constraints, k-point sampling, potential bytes/identifiers, title/format changes,
runtime changes and missing evidence. POSCAR geometric comparisons use scaled
Cartesian positions/cell, absolute tolerance 1e-10 Angstrom and zero relative
tolerance; no wrapping or symmetry equivalence is inferred. Exact byte hashes
remain alongside semantic judgments. Unknown dialects/tags, a mismatched record,
or missing runtime evidence make the overall comparison incomplete. A bare
baseline bundle cannot establish an expected runtime overlay. The
`only_incar_tags_changed` field is populated only when the full declared scope is
assessed and every other category is unchanged; a geometry, PAW, constraint or
mesh change prevents that result. It never claims effective-execution equality.

`cmw structure embed-molecule` separately handles ordinary molecular XYZ through
the existing conversion algorithm. It publishes POSCAR only, with metadata and
mapping in Scratch. See the [embedding guide](../structure/xyz-to-poscar.md).
A later full preparation can use `"source_record": "scratch/embedding/preparation.json"`
to establish exact-byte lineage to that completed embedding. The legacy converter
retains documented overwrite and adjacent sidecar behavior.

## Incomplete publication

Read the [publication protocol](preparation-publication.md) for native no-clobber
publication, exclusive Scratch ownership, staged failures, interrupted states,
cross-filesystem limits and durability caveats. If inputs were published but the
record could not be finalized, preparation fails explicitly and retains inputs
and diagnostic Scratch evidence. It does not retry with overwrite or delete the
published directory. Inspect it read-only:

```sh
cmw vasp check-inputs project/static \
  --preparation-record scratch/static/preparation.json --json
```

A non-complete record produces an invalid check even if all four input files are
present and individually parseable. Recovery is a separate human decision.
