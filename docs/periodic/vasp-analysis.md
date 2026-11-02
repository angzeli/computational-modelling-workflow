# Modular VASP SCF and electronic analysis (V1)

These read-only APIs inspect existing native evidence. They neither launch VASP,
query Jobs, finalize an artifact nor change the acceptance scope of
`inspect-result` or the live Dashboard. V1 supports the ordinary VASP 6.6.1
static/fixed-cell DAV/RMM scope and, **for these analysis APIs only**, bounded
HSE06 static DAV (`GGA=PE`, `AEXX=.25`, `HFSCREEN=.2`, `IALGO=38`). SOC,
noncollinear, general hybrids, meta-GGA, response and variable-cell modes remain
unsupported. Nonmagnetic HSE06/DAV has native-output validation. The standard
LOCPOT route was validated on one real TiO₂(101) HSE06 slab, J26.1. That replay
verified that missing native vacuum records are handled correctly; it did not
validate a successful native/LOCPOT crosscheck. Collinear and RMM coverage remains
synthetic, not a general engine qualification.

## Independent calls and shared requests

```python
from cmw.periodic.vasp.scf_analysis import (
    AnalysisContext, get_scf_history, get_scf_convergence,
    get_scf_timing, get_scf_summary,
)
from cmw.periodic.vasp.electronic_levels import (
    get_fermi_level, get_vbm, get_cbm, get_band_gap,
    get_vacuum_level, get_work_function, get_ionization_energy,
    get_electron_affinity,
)

# Each accepts a directory independently; no constructor or import opens files.
fermi = get_fermi_level("/path/to/run")
history = get_scf_history("/path/to/run", segment=0)

# Prefer one context for a set of related observations.
request = AnalysisContext("/path/to/run", eigenvalue_source="xml")
vbm = get_vbm(request)
cbm = get_cbm(request)
gap = get_band_gap(request)
```

Options belong on the context, or as keywords for a directory call. A context
is request-scoped: do not change its options. Parsed sources are reused, and
device/inode/size/mtime/ctime changes invalidate the request permanently. Start a
new context for a new observation. There is no process-global cache. Source
digests are collected by `SnapshotReader` in the parsing pass. This is sequential
evidence, not an atomic snapshot or an authenticated execution record.

Results are strict JSON-compatible dictionaries: `value`, `units`, `convention`,
`assessment`, `identity`, `source_references`, `findings`, and `qualifications`.
Missing/unqualified final values are `null`, not zero. `observed` and `details`
retain diagnostic evidence; `diagnostic=True` may expose provisional direct
values but never promotes derived quantities. Assessments use `PASS`, `FAIL`,
`UNKNOWN`, `UNSUPPORTED`, `NOT_APPLICABLE`; availability is separate. `PASS`
means the named analysis conditions hold, not managed execution acceptance,
physical accuracy, or adequate Brillouin-zone sampling. References list the
sources observed in the request, including previously requested metrics.

| Metric | Files opened on an independent default call |
| --- | --- |
| SCF history/convergence/summary | OUTCAR, present OSZICAR; explicit stdout is checked alongside it |
| SCF timing | OUTCAR only |
| Fermi | OUTCAR and electronic table; never EIGENVAL/LOCPOT |
| VBM/CBM/gap | OUTCAR, electronic table, XML if present, otherwise EIGENVAL |
| Vacuum | Native OUTCAR and electronic table; no automatic LOCPOT read |
| Work function | Vacuum and reported Fermi dependencies only |
| Ionization energy/electron affinity | Vacuum and VBM/CBM dependencies |

Explicit `outcar`, `oszicar`, `stdout`, `xml`, `eigenval`, `locpot` paths override
source locations. Select `eigenvalue_source="xml"|"eigenval"` to force the
reader, `fermi_source="outcar"|"xml"`, or
`vacuum_source="native"|"locpot"|"crosscheck"`. A selected missing, malformed or
contradictory source never silently falls back. Default XML selection does not
read an unused EIGENVAL merely because it exists.

## SCF interpretation

Multiple OUTCAR execution segments require a zero-based `segment`. Summary and
convergence select the latest chronological evaluation, including an incomplete
one, unless `evaluation` is explicit. History retains all evaluations of the
selected segment by default, without the live adapter's retention limits.
Native labels, algorithms, signed dE/d eps, printed precision and missing rms(c)
are preserved. A partial trailing record cannot erase its complete prefix or
establish a final successful result.

Numeric convergence requires **both** printed-resolution-aware energy criteria;
native stopping, minimum iterations, NELM, evaluated completion and the terminal
footer remain separate. EDIFF=0 is fixed-count behavior, not convergence.
Buffering is pending correspondence; contradictory complete rows conflict.
An explicit stdout can supply evidence beside empty OSZICAR. Complete malformed
siblings and surplus segments cannot be hidden by a valid primary source.
See [EDIFF](https://vasp.at/wiki/EDIFF) and
[OSZICAR](https://vasp.at/wiki/OSZICAR).

`LOOP` CPU/wall seconds are attached to electronic iterations; `LOOP+` to their
completed outer evaluation. Electronic sums, outer sums and whole-run footer
totals are distinct nested quantities and are never added. Missing or ambiguous
timer coverage is explicit. Boxed performance advice is not an effective setting.

## Electronic levels and correspondence

XML is streamed selectively, discarding DOS/projection/grid payloads. Its
calculation index, energies, SCF steps, settings and ordered geometry must match
the selected OUTCAR evaluation. XML/EIGENVAL bands are compared against the
complete native band counterpart, including coordinates, occupations and
printed energy intervals. EIGENVAL has no calculation identity and cannot supply
an earlier historical evaluation. These are reconstructed source
correspondences, not historical execution/input authentication.

One occupation analysis supplies all edges. Full occupation is 1 in XML and
EIGENVAL, versus 2 in nonmagnetic OUTCAR; spin degeneracy is retained separately
for electron counting. Normalized occupation tolerance defaults to `1e-3`, energy
tolerance to `1e-5 eV`; printed-resolution bounds are recorded independently.
Supported smearing for the bounded band classification is ISMEAR=-5, -1 or 0.
Intermediate occupations, band crossings, inconsistent counts, missing empty
bands and precision-boundary cases prevent an unqualified semiconductor gap.
Metallic/half-metallic diagnostic classifications never get an invented positive
gap. Global/per-spin extrema include fractional k coordinates, zero-based indices,
native labels and precision-tied states.

Only positive-weight integration points count by default; excluded zero-weight
indices are reported. `all_points=True` explicitly requests sample-qualified
extrema, not a self-consistent Fermi reconstruction. No Fermi energy is inferred.
Reported LEGACY, MIDGAP or fixed conventions remain explicit; for a semiconductor
they need not represent an experimental chemical potential. See
[BANDGAP](https://vasp.at/wiki/BANDGAP) and
[EFERMI](https://vasp.at/wiki/EFERMI). XML can print coarser eigenvalues than
EIGENVAL or the OUTCAR summary; do not report spurious extra precision.

## Vacuum references

Native LVACPOTAV upper/lower records and warnings stay separate. Supported
electrostatic semantics require LVHAR evidence excluding XC; a coordinate gap
or run name cannot establish vacuum. Missing evidence is `UNKNOWN`.
`no_vacuum=True` is an explicit caller declaration and yields `NOT_APPLICABLE`.

LOCPOT is opt-in and requires side-labelled intervals in Angstrom along a selected
lattice vector, e.g. `intervals={"lower": [1, 5], "upper": [15, 19]}`. These are
illustrative positions, not a recipe for a particular slab. Axis is zero-based
`0/1/2`, or inferred from supported native IDIPOL=1/2/3. The selected axis must be
perpendicular to the other two vectors; in-plane skew is allowed. Intervals can
wrap periodically. Ambiguous unwrap/side assignments are rejected.

A LOCPOT/native geometry discrepancy explainable only by overlapping printed
bounds remains `UNKNOWN`, not a proven conflict or a match. For a static endpoint,
V1 may read the selected XML on demand and reuse its native correspondence checks.
For unit-scale Direct LOCPOT headers, every finer XML cell/site interval must fit
inside the corresponding LOCPOT rounding interval, with matching ordered species.
This affirmative correspondence is labelled `locpot_geometry_consistent_at_source_precision`;
it does not enlarge the Cartesian tolerance or authenticate generation. Missing,
coarse, boundary-ambiguous or conflicting XML cannot resolve the geometry. Historical
LOCPOT evaluation/segment selections without identity remain unresolved.

| Plateau option | Default |
| --- | --- |
| `min_width_angstrom` | 2 Å |
| `max_variation_ev` | 0.05 eV |
| `max_abs_slope_ev_per_angstrom` | 0.01 eV/Å |
| `atomic_exclusion_angstrom` | 2 Å |
| `discontinuity_exclusion_angstrom` | 1 Å |
| `jump_threshold_ev` | 0.5 eV |

Override via the context's `thresholds` dictionary or corresponding CLI flags.
All thresholds and interval means/widths/variations/slopes are recorded. The grid
is x-fastest, then y, then z; planar sums are streamed. Potential units are eV:
there is **no density/cell-volume normalization**. Nonmagnetic scalar or
pointwise-compatible collinear electrostatic channels are supported; XC-containing,
unknown or incompatible spin representations are diagnostic/unsupported.

Source selection and LOCPOT acceptance are separate:

- `vacuum_source="native"` remains the default and never opens LOCPOT. Missing
  native values remain `UNKNOWN`, regardless of the LOCPOT policy.
- Explicit `vacuum_source="locpot", locpot_policy="standard"` (the default policy)
  accepts per-surface values when the scientific checks and positive evaluated
  geometry/source correspondence pass. Neither a receipt nor native vacuum values
  are required. Values carry `CONSISTENT_UNAUTHENTICATED`: numerical/source
  consistency is not proof of which process generated the file.
- `locpot_policy="strict"` preserves the conservative native-corroboration
  requirement. Valid native agreement can satisfy it, but still does **not**
  authenticate generation; `generation_binding` stays `unestablished`.
- `vacuum_source="crosscheck"` always requires both sources and comparable
  per-surface values, under either policy. Missing native values do not pass.

Present native contradictions/warnings are not ignored by standard mode. Unknown
potential semantics, malformed grids, changed sources, foreign geometry and
unresolved precision remain blocking. A failed surface does not suppress an
independently valid opposite surface. `surfaces` keeps `scientific_assessment`,
`source_consistency` (`CONSISTENT_UNAUTHENTICATED`, `UNRESOLVED`, `CONFLICTING`) and
`native_corroboration` separate. Qualifications accompany each surface and every
derived result/parent. `PASS` means the selected analysis policy is satisfied,
not authenticated generation or physical convergence. No verified receipt route
exists in V1; no mode produces an `AUTHENTICATED` claim. Filenames, timestamps and
user assertions alone are not positive correspondence. A stale file indistinguishable
in the available scientific evidence cannot be detected or authenticated by this
policy; known contradictions still fail. Arbitrary receipt schemas and automatic
plateau selection remain unsupported.

For each independently validated surface:

- Work function: `vacuum - reported Fermi`.
- Ionization energy: `vacuum - VBM`.
- Electron affinity: `vacuum - CBM` (negative values are retained).

Never average the surfaces. These definitions satisfy `IE - EA = gap` with a
shared reference. Derived values require compatible final parents even in
diagnostic mode. See [LOCPOT](https://vasp.at/wiki/LOCPOT),
[LVACPOTAV](https://vasp.at/wiki/LVACPOTAV), and
[work function](https://vasp.at/wiki/Computing_the_work_function).

## CLI

```sh
cmw vasp analyze-scf RUN_DIRECTORY --json
cmw vasp analyze-scf RUN_DIRECTORY --only history,convergence,timing --json
cmw vasp analyze-electronic RUN_DIRECTORY --only fermi,vbm,cbm,gap --json
cmw vasp analyze-electronic RUN_DIRECTORY --only work-function --json
cmw vasp analyze-electronic RUN_DIRECTORY --only gap --eigenvalue-source eigenval --json
cmw vasp analyze-electronic RUN_DIRECTORY --only vacuum,work-function \
  --vacuum-source crosscheck --axis 2 --lower-interval 1 5 --upper-interval 15 19 --json
```

J26.1 read-only qualified alignment: set `RUN_DIRECTORY` to that run directory.
These independently inspected windows are specific to this TiO₂(101) slab:

```sh
cmw vasp analyze-electronic "$RUN_DIRECTORY" \
  --only vacuum,work-function,ionization-energy,electron-affinity \
  --vacuum-source locpot --locpot-policy standard --eigenvalue-source eigenval --axis 2 \
  --lower-interval 1 3 --upper-interval 39.066649 41.066649 --json
```

Use `--locpot-policy strict` to require corroboration. The reported J26.1 9–23 meV
window sensitivity is not a total physical error bar. `IE − EA = gap` checks the
formulas, not provenance. Policy changes invalidate an already used context;
construct a new context rather than reusing a cached request under new options.

Electronic `--only` is mandatory. Every requested metric is emitted even if a
different metric is unavailable. Exit 0: requested analyses satisfied (including
explicit not-applicable results); 1: invalid evidence/failed required scientific
check; 2: unknown, unsupported or invalid request. The command never writes a
report beside scientific files. Redirect user-requested reports to separate
Scratch storage. `--help` lists source/selection/tolerance options.

Tests use synthetic fixtures and do not execute scientific engines. Native HSE06
validation does not establish LOCPOT provenance, Jobs binding, relaxed structures,
sampling adequacy, a minimum or a quantitative work function where no vacuum
output exists. A historical runner record pointing to a different output directory
remains an unbound record; analysis never relocates or adopts it.
