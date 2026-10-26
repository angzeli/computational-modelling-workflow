# Reviewed ORCA and Multiwfn Jobs submissions

`cmw jobs check --spec SPEC.json --json` is a read-only preflight. It does not
create Jobs state, prepare inputs, probe an engine, or authorize execution.
`submit --preview` uses the same check locally. `submit` intentionally prepares a
new private attempt and registers it on **Hold**. Existing generic `jobs add`
semantics are unchanged.

```sh
cmw jobs --state /absolute/private/jobs check --spec /absolute/spec.json --json
cmw jobs --state /absolute/private/jobs submit --spec /absolute/spec.json --preview --json
cmw jobs --state /absolute/private/jobs submit --spec /absolute/spec.json --json
cmw jobs --state /absolute/private/jobs release J1.1
cmw jobs --state /absolute/private/jobs start
cmw jobs --state /absolute/private/jobs progress J1.1 --json
cmw jobs --state /absolute/private/jobs progress J1.1 --history --json
cmw jobs --state /absolute/private/jobs files J1.1 --json
cmw jobs --state /absolute/private/jobs show J1.1 --json
cmw jobs --state /absolute/private/jobs watch
```

Use the actual returned Job ID. Release and dispatch are separate explicit
operations. A held queue head preserves the existing ordering contract.
Submitting the same UUID and semantic specification returns the original attempt;
a conflicting reuse of the UUID is refused. Preparation refuses an occupied
working directory and retains evidence if a later registration fails.

## Local specification

The schema has exactly these required fields (plus optional `note`):

```json
{
  "schema": 1,
  "request_id": "a9682b71-5fbb-4701-b78c-4e0be1880374",
  "engine": "ORCA",
  "name": "Reviewed single point",
  "intent": "held",
  "resources": {"cpus": 2, "memory_gib": 8, "mpi_ranks": 2, "threads_per_rank": 1},
  "workdir": "/absolute/new-private-attempt",
  "runtime": {},
  "scientific": {}
}
```

Populate `runtime` and `scientific` from the engine contracts:
[ORCA Jobs](molecular/orca-jobs.md) and
[Multiwfn Jobs](molecular/multiwfn-jobs.md).
Paths are explicit absolute paths; no shell expansion, executable discovery or
scientific parameter selection occurs. Engine labels remain ORCA and Multiwfn.

Preflight findings distinguish ERROR, UNSUPPORTED, UNASSESSED and WARNING.
Only READY can prepare/register. Missing evidence is not a guessed scientific
failure. A synthetic qualification labels test substitutes; it does not relax
source, resource, byte identity, menu or validation checks.

## Execution and observation

The immutable Jobs record binds resources, argv, environment, working directory,
original sources, prepared inputs, runtime evidence and existing runner assets.
The worker checks dependency bytes at actual admission; a changed dependency
returns the job to Hold before the payload is released. Dashboard advisory checks
do not repeatedly hash scientific files. Existing wrappers own foreground child
processes, cleanup and signals; existing Python components own scientific
preparation and finalization.

ORCA observes the exact stage output or prepared workflow state. Multiwfn observes
its exact native session, bounded runner events, an immutable pinned association,
and a separate final-validator projection. The shared Scientific Progress frame,
Files, History, Copy, Sources and notes remain in use. No new panel, controls or
engine parser are added to the frontend.

`jobs show --json` exposes the exact existing finalizer record separately as
`scientific_result` for terminal attempts. It labels that record as recorded
evidence, not fresh artifact revalidation; missing records remain unavailable.

Jobs Done describes process outcome. Native completion, output identity,
scientific validation and reuse eligibility remain distinct. Reading a progress
stream never finalizes or adopts a result. A runner validator record reports the
finalizer's recorded outcome; it does not rehash products on every display poll.

These reviewed routes do not opt into Controlled Suspend/Continue. Existing
explicitly enrolled ordinary ORCA SP/OPT suspension routes remain available under
their separate contract. Multiwfn and whole multi-stage/frequency workflows have
no newly claimed suspension eligibility. No job is automatically an Auxiliary.

## Explicit execution nodes

Use the [dual-node contract](cmw-dual-node.md) for enrollment and portable specs.
The selected profile supplies the owning node's pinned runtime. Client machine
paths cannot stand in for destination paths.

```sh
cmw jobs --node mini --queue research check --spec /absolute/remote-spec.json --json
cmw jobs --node mini --queue research submit --spec /absolute/remote-spec.json --preview --json
cmw jobs --node mini --queue research submit --spec /absolute/remote-spec.json --json
cmw jobs --node mini --queue research release J1.1
cmw jobs --node mini --queue research start
cmw jobs --node mini --queue research progress J1.1 --json
cmw jobs --node mini --queue research files J1.1 --json
```

Pure remote pretransfer checks explicitly mark unavailable destination input bytes
UNASSESSED. Full scientific preflight runs on verified published bytes before
registration and on bound dependencies at final admission. Target-local donor
references require the exact owning node, queue, Jobs attempt, Files location,
role, relative path, digest and scientific result/attempt identity. A copied
manifest is not provenance adoption. Fetch creates a verified replica under the
existing selected-artifact contract, not a new scientific source authority.

## Installation and qualification

Core and Jobs wheels package the same existing shell runner sources and their
small helper closure. Installed assets use the selected installed Python without
adding an unrelated checkout to PYTHONPATH. Checkout/sdist runners bind their own
adjacent `src` package. Engines, MPI, converters and licenses are supplied
separately. Converter operation is explicit and is not part of checking or
submission.

Synthetic local/endpoint/loopback SSH tests establish software integration only.
Real ORCA/Multiwfn, native MPI and physical MacBook/Mac mini qualification remain
separate acceptance work. No host-wide reservation or cross-node MPI is implied.
