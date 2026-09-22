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
cmw vasp potcar build --pot In=In_d --dry-run --json
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

## Selection identity and comparability

Build and list now reject a selected directory whose parsed dataset variant is
different, even when both identify the same element. This deliberately tightens
acceptance of demonstrably mislabeled library entries. An explicit `--pot`
selection also requires a parsed variant; VRHFIN-only data cannot prove that
request. Missing variants still fail without a fallback.

Metadata interpretation is bounded: `TITEL` supplies a family token, a variant
token such as `In_d`, and an optional `DDMonYYYY` dataset date. Additional title
text is not compared as a complete string. `VRHFIN` supplies element identity.
Missing family/variant/date evidence remains unestablished. A date never establishes
the release of an entire library, and metadata parsing does not validate numerical
payloads or scientific suitability.

Ordinary `check` retains element-only inspection when only VRHFIN is available.
An ordinary suffix-free build may also retain that legacy mode and records
`selection_status: element_only`. `--strict-identity` requires variant and family
metadata. Dataset dates and library releases are required only when explicitly
requested. No family, date, release, or material-specific variant is hardcoded.

`--requirements PATH` on build/check reads a JSON object with these optional keys:

```json
{
  "family": "INVENTED",
  "dataset_date": "01Jan2001",
  "library_release": "synthetic-release-2",
  "variants": {"In": "In_d"}
}
```

This example is invented metadata, not a recommended potential choice.
`sha256` may additionally map element symbols to lowercase SHA-256 identifiers
of parsed dataset byte spans, including their `End of Dataset` line. Requirements
apply to every repeated block of that element. Unknown requirement keys fail.
Supply `--library-release synthetic-release-2` separately when that release is
established by caller records; CMW labels its basis `caller_declared`, does not
infer it from dataset dates, and does not independently certify that declaration.
Without this separate evidence, a requested release remains unestablished.

```sh
cmw vasp potcar build POSCAR --pot In=In_d --potcar-root /path/to/library \
  --strict-identity --dry-run --json
cmw vasp potcar check POSCAR --potcar POTCAR --requirements requirements.json \
  --library-release synthetic-release-2 --json
```

Build preview reads only selected sources and writes nothing: no POTCAR,
temporary file, directory, or metadata sidecar. It checks destination prerequisites
and overwrite policy as well as selection. Check is always read-only. `--json`
on build/check returns non-payload records; list also supports JSON. Exit 0 means
the requested supported checks passed, not scientific approval. Unmet requested
identity checks and malformed inputs return nonzero (2 through the canonical CLI).
An assembled POTCAR alone does not establish which library selection or lineage
produced it; check reports selection and scientific suitability as unassessed.

The structured selection contains full ordered species blocks and counts,
block-to-source mapping, requested/resolved variants, parsed family/date,
comparability evaluations, unestablished fields, and exact content identities.
`source_sha256` covers the complete selected source file. `assembled_chunk_sha256`
covers that source after the existing final-newline separator rule. Dataset hashes
cover parsed terminated spans; `potcar_sha256` covers the exact assembled bytes.
These differ when source padding or the final-newline rule changes boundaries.
Only selected files are hashed; repeated blocks reuse the same read snapshot.

## Python and full-preparation integration

`resolve_potcar(poscar, root=..., pots=..., requirements=..., strict=True)` accepts
a POSCAR path or bytes and returns `PotcarSelection`: validated `.content`, a
JSON-safe `.record`, and `.source_paths`. Publication uses this exact byte snapshot,
without rereading changed library files. `inspect_potcar(data, species=..., counts=...,
requirements=..., strict=...)` inspects assembled bytes; failed identity requirements
set `identity_valid` false, while malformed metadata/order raises `ValueError`.
The resolver/build refuses failed identity requirements before publication.

The full VASP preparer embeds this record in Scratch `preparation.json`; the
repository-side calculation directory contains only INCAR, KPOINTS, POSCAR and
POTCAR. Standalone POTCAR commands continue to produce no unsolicited sidecars.
These APIs and canonical commands are package-contained and need no checkout,
scientific executable, queue, or real potential library in synthetic tests.
Donor splitting, restart compatibility and scientific-result validation remain
outside this implementation.
