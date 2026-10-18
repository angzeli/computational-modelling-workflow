# Jobs location declarations and folder navigation

This contract supplies reusable location evidence for clients and future engine
adapters. It does not parse progress, validate scientific results, change Jobs
state, or establish process ownership. No terminal receipt is required to discover
a running job's declared output.

## Result and identity

`cmw.jobs.locations.discover(store, job_id, *, expected_attempt_id=None,
descriptors=(), resolvers=None)` returns an immutable `LocationResult`; `to_dict()`
is JSON-compatible and has `schema: 1`.

Each `Location` records a stable key, canonical state directory and database
identity, immutable internal job and attempt IDs, role, absolute canonical path,
original declared path, directory/file kind, optional stage, declaration origin,
relationship qualification, accessibility and diagnostic detail. A display label
such as J55.1 alone is never a global identity. Keys include state identity,
attempt, path, stage, role and declaration origin. Filesystem object identities
are retained for revalidation; changing file contents does not itself change a
location key. A redirected symlink or replaced selected object must not inherit
the old selection's binding.

`process_association: not-established`, `process_ownership: false` and
`scientific_validation: not-assessed` explicitly describe the limits of discovery.
The scientific finalizers retain their stronger identity, source, completion and
acceptance requirements. A declared path remains navigable when independent
process association is unknown.

## Supported declarations

| Route | Exposed locations | Boundary |
|---|---|---|
| Persisted Jobs record | Working directory, stdout and stderr | cwd is never implicitly output; no process/controller probe is needed |
| Explicit Jobs execution layout | Output/inputs, provenance and log locations, linked layout record | Existing layout identity checks and migration contract apply |
| `run-vasp.sh` argv | Explicit `--input` and `--output` | Known argv grammar only; no script execution, restart inference or finalized receipt required |
| `run_orca.sh` argv | Explicit input/output/stderr, target, finalization metadata, layout, geometry/runtime records and declared artifacts | Output files navigate through their containing directory; target metadata may identify the stage |
| `run_opt_freq_sp.sh` argv | Explicit workflow root, structure and configuration | Individual hashed stage paths remain unknown without exact recorded metadata |
| `python -m cmw.molecular.workflows.cli next/advance --state …` | Recorded stage runner/artifact locations and metadata | Only the explicit v1 OPT/FREQ/SP state record is read; stage/target/attempt relationships are checked |
| Explicit caller descriptors / registered resolvers | Caller-declared roles and exact paths | Session-local API below; no storage schema or registry is added |

The VASP grammar includes `--managed-foreground`, `--controlled-suspension`
and the value-bearing `--execution-budget SECONDS` option, including `=VALUE`
syntax. This only interprets declared locations: budget values, suspension
eligibility, input checks and no-clobber policy remain the runner's validation
responsibility. Missing values, duplicate non-repeatable options and unknown
options remain unresolved.

Known shell-interpreter prefixes are accepted only for literal script argv,
including Jobs' `-e -o pipefail`. Shell `-c`, arbitrary Python, opaque wrappers and
unrecognized options remain unresolved. Declared path characters are literal:
there is no shell-variable expansion, script sourcing, mtime-based selection,
research-tree globbing or basename-based archive replacement. No CP2K resolver
is included. A recognized argv dialect is a declaration, not authentication of
an executable that happens to share its name.

## Selection, accessibility and opening

`preferred_key` identifies one unique output destination only when the discovered
stage context is unambiguous. A missing preferred output remains an error, with
other locations offered separately. Multiple stages/destinations are `AMBIGUOUS`;
no declared output is `LOCATION_UNKNOWN`. Equivalent output declarations may
point to the same destination while retaining their separate origins.

Accessibility reports `AVAILABLE`, `NOT_CREATED_YET`, `NOT_FOUND`,
`PERMISSION_DENIED`, `VOLUME_UNAVAILABLE` or `LOCATION_UNKNOWN`, according to
observed evidence. `NOT_CREATED_YET` requires a declared generated path and an
unstarted Queue/Hold attempt; missing paths in other lifecycle states are not
assumed to be future outputs. A missing `/Volumes/NAME` mount point can establish
`VOLUME_UNAVAILABLE`; no mount, permission change, directory creation or repair is
attempted. Unexpected filesystem errors retain their diagnostics.

`cmw.jobs.folder_opener.open_location(store, location, ...)` re-reads the Jobs
identity and declaration and checks the current target before opening. Source
files open only their verified parent folder. The macOS opener uses an argv
array, `/usr/bin/open -- ABSOLUTE_DIRECTORY`, with a timeout and no shell. Invalid
URIs, non-directory open targets, changed selections and changed symlink targets
are refused. Application, script, settings and installer directory bundles are
also refused to prevent the desktop opener dispatching executable content. The API reports `LOCATION_CHANGED`, `UNSUPPORTED_DESKTOP`,
`OPEN_FAILED` or the underlying accessibility result as appropriate. Only an
accepted OS request yields `OPEN_REQUESTED`; Finder appearance is not asserted.
Full paths remain available if opening is unsupported or fails. No clipboard
integration or terminal file manager is added.

The TUI uses mouse cell hit-testing at click time, independent of row-cursor
messages. Asynchronous requests are bound to the selected attempt, visible
context and location key. One Files request runs at a time. Selection, resize,
order, disappearance and declaration changes invalidate stale results. A chooser
shows the role, stage, complete path and relationship qualification for each
entry; its buttons are explicit open actions.

CLI listing exits 0 even when locations are unknown: uncertainty is returned as
data. Missing/invalid job identity exits 2 with the existing `JOBS_ERROR` JSON.
`--open` exits 0 only for `OPEN_REQUESTED`, otherwise 2 with the location list and
an `open` result. `--role` or `--key` filters listings; with `--open` it must select
one entry. `--open` without either selects only `preferred_key`. Listing/JSON
never invokes an opener or backfills missing metadata. Core commands work without
Textual or Rich.

## Scientific source bindings remain separate

F Files exposes declared navigation locations. S Sources and the
[progress bridge](cmw-jobs-progress.md#association-and-sources) reuse those
locations to reconcile exact registered scientific roles with immutable add-time
source declarations. Compatible declarations are observed automatically;
conflicting required sources have no silent winner. Opening a Files folder does
not select it as scientific evidence. Missing or inaccessible declared paths
retain their association and accessibility result.

Session-local Sources selections and `add --progress-source` metadata do not
rewrite location records or create a persistent location registry. An explicit
selection may identify a readable output without establishing execution lineage,
process ownership or scientific provenance. The existing location identity and
folder-opening checks continue to apply independently.

## Caller and future-engine seam

```python
from cmw.jobs.locations import LocationDescriptor, discover
from cmw.jobs.folder_opener import open_location
from cmw.jobs.store import Store

store = Store('/absolute/private/jobs-state')
declarations = (
    LocationDescriptor('output', '/absolute/declared/run', stage='analysis'),
    LocationDescriptor('records', '/absolute/declared/preparation.json',
                       kind='file', stage='analysis'),
)
locations = discover(store, 'J1.1', descriptors=declarations)
# Only after the user explicitly selects an entry:
selected = next(item for item in locations.locations
                if item.key == locations.preferred_key)
# open_location(store, selected, descriptors=declarations)
```

Descriptors apply to that explicit call and must be supplied again when opening.
They are not persisted. Use `expected_attempt_id` when acting on an earlier job
selection. A client must not derive descriptors from names/notes or transfer a
location selection between states.

A resolver is a callable receiving the persisted job mapping and returning
`LocationDescriptor` values. Supply `resolvers=(my_resolver, ...)` to replace the
small default registry, or compose `builtin_resolvers()` from
`cmw.jobs.location_resolvers` with an additional callable. No generic opener,
TUI or scheduler engine branch is needed. A resolver must use bounded, explicit,
read-only evidence; it must not execute commands or scan for plausible outputs.
The fictional resolver regression establishes this extension point without
adding another production engine. A persistent descriptor route is a separate
future design decision.
