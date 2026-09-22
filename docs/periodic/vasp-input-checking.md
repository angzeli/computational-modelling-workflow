# Read-only VASP input checking

`cmw vasp check-inputs INPUT_DIRECTORY --json` inspects `INCAR`, `KPOINTS`,
`POSCAR`, and `POTCAR`. It writes no files, changes no inputs, and launches no
calculation. A directory containing exactly these four files is sufficient.
There is no required metadata sidecar in the input directory.

An optional `--preparation-record /explicit/scratch/preparation.json` checks the current
bytes against a separately stored preparation record and its publication state.
The record must have schema version 1 and kind `vasp-input-preparation`.
Its `selected_potentials` record, when present, also supplies the expected ordered
potential identities. Checking never repairs incomplete publication or restores
modified inputs.

## Result and exit status

The API functions `check_input_bytes(files, expected_selection=None)` and
`check_inputs(directory, record_path=None, expected_selection=None)` return
JSON-compatible results with `status`, `exit_code`, `findings`, parsed `inputs`,
and content `identities`. Each finding has a stable `code`, `severity`, `scope`,
`message`, and `evidence`.

| Status | Exit | Meaning |
|---|---:|---|
| `valid` | 0 | No malformed/inconsistent input or unsupported feature found in the bounded checks |
| `invalid` | 1 | Malformed or inconsistent input, failed required identity, unreadable file, or inconsistent external record |
| `unsupported` | 2 | A requested feature is outside the supported checker |

Errors take precedence over unsupported features. Warnings and unassessed
properties do not cause a nonzero exit. A zero exit does not imply complete
keyword coverage, a valid physical model, convergence, or runtime acceptance.
Without an external record, lineage and publication state are explicitly
unassessed. Without expected potential requirements, element-order agreement is
not presented as verified selection or comparability.

## Supported interpretation

- POSCAR: explicit VASP 5/6 element blocks, including repeated blocks and zero
  counts, with a positive total atom count; one positive scale, one negative
  target-volume scale, or three positive scale factors; finite nondegenerate
  cells; Direct and Cartesian coordinates; selective dynamics with three T/F
  flags per atom. Raw text, atom sequence, block counts, and coordinates are
  preserved. Coordinates in the parser summary remain in the original declared
  representation; `cell` includes scaling and `cartesian_coordinates` exposes
  scaled positions in Angstrom for representation-independent comparison.
  There is no sorting, regrouping,
  wrapping, or centering. Implicit VASP 4 identities, coordinate suffix fields,
  velocities, and other meaningful trailing sections are unsupported.
- INCAR: case-insensitive keys, `#`/`!` comments, semicolon-separated assignments,
  quoted SYSTEM text, booleans, scientific notation including D exponents, and
  numeric repeated-value arrays. Identical duplicate assignments produce a
  warning; conflicting duplicates are invalid. Unknown tags are retained and
  explicitly unassessed. SYSTEM is descriptive text, never a source of mode
  settings. Named settings retain numeric-looking identifiers such as `GGA=91`.
  Backslash-continued assignments are reported as unsupported and their continued
  lines are not interpreted as independent malformed assignments.
- KPOINTS: automatic Gamma and Monkhorst-Pack regular meshes with three positive
  integer dimensions and three finite shifts (omitted shift means zero).
  No universal slab-normal, third-dimension, or centering restriction is imposed.
  Line mode, explicit lists, automatic-length mode and KPOINTS_OPT are unsupported.
- Cross-file checks: every POSCAR species block must correspond to a POTCAR
  dataset in the same order. Collinear MAGMOM has one entry per atom; LDAUL,
  LDAUU and LDAUJ have one entry per species block, including repetitions;
  DIPOL has three components. Noncollinear/SOC arrays are unsupported. Positive
  ionic steps with disabled ionic motion are inconsistent; other ionic modes
  beyond explicit fixed-cell IBRION 1/2/3 and ISIF 2 are unsupported. Checkpoint
  restart and frozen-density modes are outside this four-input contract.

`expected_selection` accepts the POTCAR component's ordered selection record or
its explicit requirements mapping. The checker reuses the existing POTCAR parser
and identity rules; it does not split or reassemble donor datasets. A dataset
date does not establish a library release. Required unavailable identities fail.
Only non-payload potential identifiers appear in results.

The separate preparation command additionally requires caller-owned scientific
profile context and supported explicit mode/initialization settings. A generic
check does not invent those settings when they are absent.
