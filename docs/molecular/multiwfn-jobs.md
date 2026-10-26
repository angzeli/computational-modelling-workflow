# Multiwfn in CMW Jobs

The reviewed `Multiwfn` submission route connects the existing FMO, ESP, IGMH and
two-fragment IFCT workflows to ordinary local Jobs. It preserves their native
menu renderers, foreground child ownership, runtime aliases, output validators
and finalizers. Jobs registration defaults to **Hold**. Check and preview never
launch Multiwfn, probe its version, create an analysis attempt or start a queue.

## Check, prepare and register

A schema-1 Jobs spec has `engine: "Multiwfn"`, a UUID `request_id`, a literal
`name`, `intent: "held"`, an absolute new `workdir`, explicit `resources`,
`runtime` and `scientific` objects. The shared commands are:

```sh
cmw jobs check --spec analysis.json --json
cmw jobs submit --spec analysis.json --preview
cmw jobs --state /path/to/private/jobs submit --spec analysis.json
```

Submission is the intentional preparation operation: it materializes the exact
scientific attempt and stdin/settings, then registers the held job with those
known future native log paths. It does not guess a future `attempt_NNN` directory
or observe the final copyback destination. The scientific target/attempt and the
Jobs immutable attempt remain separate associated identities. A repeated request
with unchanged semantics returns the same registered attempt; changed semantics
under that ID are refused.

After review, ordinary Jobs release and start remain separate actions:

```sh
cmw jobs --state /path/to/private/jobs release J1.1
cmw jobs --state /path/to/private/jobs start
cmw jobs --state /path/to/private/jobs progress J1.1 --json
cmw jobs --state /path/to/private/jobs progress J1.1 --history --json
cmw jobs --state /path/to/private/jobs files J1.1 --json
```

For node-selected submission, use the explicit node/queue route documented in
[dual-node Jobs](../cmw-dual-node.md). A remote donor is a registered target-local
artifact reference, not a caller-provided arbitrary remote pathname. Copying a
source manifest and replacing its paths does not establish relocated provenance.
Cross-host donor adoption remains unsupported by this integration.

## Runtime and resources

The runtime object requires:

| Field | Meaning |
| --- | --- |
| `qualification` | `recorded`, or explicitly `synthetic` for harmless qualification fixtures |
| `executable`, `sha256` | Target-local executable and reviewed bytes |
| `version` | Version from prior runtime evidence; checking never launches a version probe |
| `menu_contract` | `multiwfn-3.8-compatible-v1` |
| `settings_source`, `settings_sha256` | Original target-local settings and reviewed bytes |
| `threads` | Explicit positive count, equal to Jobs CPUs |
| `contract` | Required for `recorded`: existing PASSED runtime-compatibility JSON with matching executable, version, menu, settings and thread identities |

Cube menus support the existing 3.8.x and exact 2026.7.15 contracts. IFCT requires
exact 2026.7.15. Unknown versions or menus are **UNSUPPORTED**, while absent runtime
evidence is **UNASSESSED**; neither is eligible for first-class submission.
Synthetic qualification never bypasses dependency, scientific-source or resource
checks and does not qualify a real installed engine.

Resources require `cpus`, `memory_gib`, `mpi_ranks: 1`, and
`threads_per_rank` equal to `cpus`. Thread defaults are never inherited from the
legacy eight-thread workflow default. The existing runtime creates a read-only
attempt-local settings derivative with that exact thread count; installation
settings remain unchanged. A positive total memory commitment is mandatory but
is not a Multiwfn hard limit or an inferred estimate of grid memory use. Select
an adequate commitment for the explicit grid; CMW does not silently reduce it.

Runtime identity is streamed with the shared 8 GiB file bound, independent of
portable input-transfer limits. Settings, fragment and configuration metadata
are bounded at 1 MiB. IFCT's existing terminal ORCA-state parser is limited to a
64 MiB declared output for this route. Larger scientific wavefunctions are hashed
without loading their contents into the observer or protocol response.

## Profile inputs

Every profile requires `scientific.source_result`: an original authoritative,
reusable version-1 result with successful execution, VALID scientific status,
matching target and ordered geometry, intact wavefunction/geometry artifacts,
compatible explicit wavefunction format and an exact source attempt identity.
The existing source validator remains authoritative. A bare GBW is not a
Multiwfn input: conversion must be an explicit earlier operation with the existing
ORCA conversion provenance contract.

| `scientific.profile` | Additional fields and bounded contract |
| --- | --- |
| `FMO` | `grid_spacing_bohr`; existing restricted singlet, contiguous one-based HOMO/LUMO identities |
| `ESP` | `grid_spacing_bohr`; paired density/ESP cubes |
| `IGMH` | `fragments_file`, `configuration_file`; existing public one-based complete two-fragment partition and explicit grid/configuration |
| `IFCT` | `orca_output`, `spin_manifold`, `state_index`, `atom_count`, `fragments`; exact ORCA TDA state and 2026.7.15 two-fragment Hirshfeld profile |

IFCT fragments use the existing internal spec form
`[{"label":"A","indices_zero_based":[0,1]}, ...]`. The renderer alone converts
these indices to the native one-based menu. Geometry atom count, complete
partition, ranges and overlap are checked. `orca_output` must be the authoritative
source result's output artifact; for a converted wavefunction, the original
conversion parent result, hash and attempt must still match. An unrelated
hash-equal output copy is not accepted as that original artifact.

NTO and non-fragment hole/electron remain versioned renderer/parser/finalizer
**APIs**, not these four first-class Jobs profiles. Their existing functionality
and explicit state-mapping rules are unchanged. See
[the excited-state contracts](multiwfn-3.8-excited-state-analysis.md).

## Observation and results

Four exact source declarations are automatic: native session log, optional
runner-event stream, optional validator projection, and a pinned immutable
preparation association. Missing future native logs remain pending. Runner and
validator source/attempt identities must match that pinned association before
those records can be shown. Bare explicitly selected native logs can show bounded
native markers, without accepting unbound runner or validator claims.

The existing Scientific frame uses adapter-defined **ANALYSIS**, **STAGE** and
**OUTPUT** groups. Native version/source-load markers and the demonstrated IFCT
state/matrix markers remain separate from runner actions. A launch-request event
says only that the wrapper is about to call its existing foreground launch helper.
No menu count, CPU usage, file age or log pause becomes a percentage, SCF count,
ETA, completion or stall diagnosis. Partial lines remain pending; replacement,
truncation, malformed complete records and incompatible source identities retain
qualified observation states. History, source selection, Files and Copy use the
same generic observation infrastructure.

The established cube or IFCT finalizer runs in its existing wrapper after the
child exits. Its compact validator projection names the result record and digest;
the observer reports **validator-recorded PASSED**, never performs finalization
or rehashes scientific products on each poll. Jobs Done/process exit, native
markers, output identity/completeness and task-specific validation remain distinct.
Inspect the declared `result` and scientific-attempt files for full terminal
evidence. Exit zero with incomplete cubes, mismatched IFCT sources/menu/settings,
or unsupported native output does not create a valid result record.

These profiles use normal Jobs cancellation and existing foreground cleanup.
They are not enrolled for Suspend/Continue and are not automatically auxiliary
Bounded Sharing jobs. Read-only observation and missing thermal telemetry do not
change execution eligibility.

## Installed runners

The same checked shell sources are packaged as runner assets. In a source checkout
they bind that checkout's package; installed assets clear inherited `PYTHONPATH`
and use the selected installed interpreter. They never import a neighbouring
checkout as an implicit fallback. Missing assets are detected during checking.
Real-engine and physical dual-Mac qualification remain separate from synthetic
local and isolated endpoint/SSH acceptance.
