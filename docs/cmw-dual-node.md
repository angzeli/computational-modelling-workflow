# Independent CMW execution nodes

CMW nodes expose remote access to separate local Jobs runtimes. Every node owns
its queue, controller, workers, admission decisions, scientific readers and
storage. The frontend receives observations and sends explicit requests. It does
not open remote SQLite, synchronize live state, pool resources or run cross-node
MPI. Closing an observer does not change execution state.

## Enrollment and observation

Ordinary `cmw jobs` commands retain local behavior and do not use the network.
Explicit `cmw nodes enroll --config NODE_CONFIG --spec NODE_SPEC.json` enrolls
local hardware, private queue states, directly attached storage and pinned runner
profiles. Enrollment refuses a live controller or active/unresolved attempt;
existing local states are not migrated by observation. Enrolled queues use Jobs
schema 4 so older runtimes fail closed instead of bypassing new preflight rules.
Retain old immutable runtimes for their existing unenrolled queues.

The node specification contains named `queues` (absolute private state paths),
`storages` (existing canonical directory roots), and `profiles` (engine, runner,
max_cpus, optional max_ranks and explicit dependencies). Node/queue/storage IDs are
independent UUIDs. Hardware identity, volume UUID and current mount evidence are
observed locally. A same-named replacement root is not the enrolled storage.
Neither enrollment nor readiness creates a missing calculation mount point.

`cmw nodes connect ALIAS --config FRONTEND_CONFIG --spec CONNECTION.json` checks
and enrolls an explicit connection. Its transport is `ssh` or local; SSH requires
an existing SSH alias, fixed absolute endpoint executable/configuration paths,
expected SSH SHA256 host-key fingerprint, persistent node/queue IDs, host ID and
actual runtime fingerprint. Hostnames and package version `0.1.0` are insufficient.
Aliases are display labels; all selected targets include node, queue, internal job
and immutable attempt identity.

```console
cmw nodes list --config FRONTEND_CONFIG --json
cmw nodes check mini --config FRONTEND_CONFIG --json
cmw jobs --nodes-config FRONTEND_CONFIG --node mini --queue research status --json
cmw jobs --nodes-config FRONTEND_CONFIG --node mini --queue research progress J1.1 --json
cmw jobs --nodes-config FRONTEND_CONFIG --node mini --queue research files J1.1 --json
```

## Protocol and observation boundaries

OpenSSH uses no PTY, batch authentication, strict host-key checking, no agent
forwarding and bounded timeouts. Only fixed, shell-quoted enrollment settings
cross the remote command-string boundary. Job names, paths and argv travel as
JSON data. The foreground `cmw nodes endpoint --config ... --queue ...` serves
protocol-1 JSON-lines with a 2 MiB frame bound, one in-flight request per channel,
strict request IDs/typed data and bounded diagnostic retention. Errors on stdout
are structured; no arbitrary shell/Python callback is exposed.

The endpoint retains bounded scientific observation sessions across requests.
Scientific source selection and validation, history reconstruction and log reads
stay on the owning node. The internal snapshot codec retains electronic notation
and existing bordered Scientific rendering metadata. Files include owning-node
and storage identities, relative path and node-local path; none is a permission
to open a same-spelled local path. Share opening is refused until endpoint/share
identity can be verified; Copy Location and explicit fetch are independent routes.

Snapshots separate endpoint observation sequence/session, node-local evidence
clock and transport receipt age. A reconnect does not adopt an old observer's
cursor or silently retarget a selection. Readiness distinguishes implementation,
configuration and qualification; no real-engine or physical dual-Mac acceptance
is implied by an isolated endpoint test.

## Reviewed held submission

Submission uses the same selected backend as observation. `submit --preview`
prepares a private frontend spool and durable request record, then asks the
endpoint for a read-only plan. It does not stage files on the destination or add
jobs. The manifest records each selected regular file, its origin, exact digest,
size, role and allowed mode. The frontend requires an explicit assertion that
supporting dependencies have been reviewed; it does not interpret shell scripts.

A frontend specification replaces the wire manifest with explicit `files`:

```json
{
  "schema": 1,
  "request_id": "11111111111111111111111111111111",
  "node_id": "REPLACE_WITH_ENROLLED_NODE_UUID_HEX",
  "queue_id": "REPLACE_WITH_ENROLLED_QUEUE_UUID_HEX",
  "storage": "scratch",
  "profile": "cat",
  "engine": "Command",
  "name": "Benign selected-file example",
  "resources": {"cpus": 1, "memory_gib": 0.1},
  "intent": "held",
  "dependencies_complete": true,
  "files": [{"source": "/absolute/local/hello.txt", "path": "inputs/hello.txt", "role": "input"}],
  "arguments": [{"bundle": "inputs/hello.txt"}]
}
```

The target's explicitly enrolled `cat` profile must pin `/bin/cat`, engine
`Command`, and `max_cpus: 1`. Generate a fresh request UUID for each distinct
submission; retain the same UUID and document for a retry. Literal arguments
remain literal. File references use `bundle` (selected immutable input), `run`
(destination work directory), or `dependency` (index in the pinned target
profile's dependency list). No frontend environment is copied.

```sh
cmw jobs --nodes-config /private/frontend/config --node mini --queue research submit --spec submission.json --preview
cmw jobs --nodes-config /private/frontend/config --node mini --queue research submit --spec submission.json
cmw jobs --nodes-config /private/frontend/config --node mini --queue research request 11111111111111111111111111111111 --json
```

The SSH route uses installed OpenSSH SFTP in a separate process, with strict
host checking and the enrolled fingerprint verified before batch commands are
sent. It uploads flat, private spool files to an endpoint-generated incoming
path. Publication verifies content and volume identity and promotes without
clobbering on the same filesystem. Interrupted publication is recoverable with
the same manifest and request identity. Current bounds are 128 files, 64 MiB per
file and 256 MiB total. WAVECAR/CHGCAR restart transfer is refused in this MVP.
POTCAR transfer requires the explicit `potcar_authorized: true` declaration;
no engine or potential-library distribution route is provided.

Only a verified published bundle can be registered. Registration uses one SQLite
transaction for both the new held attempt and the permanent request outcome.
Matching retries return the original outcome; changed semantics conflict. This
is duplicate-registration suppression, not distributed exactly-once execution.
A transferred bundle without registration cannot run. A default-held job also
cannot run while its controller is online. Release and dispatch remain separate
explicit operations, and a held queue head retains the existing blocking order.

Managed VASP specifications use engine/profile `VASP`, exactly four `inputs/`
files (INCAR, POSCAR, KPOINTS, POTCAR), and explicit `vasp` fields `ncore`, `kpar`,
`mpi_mode`, `timeout`, `stop_before`. The recognized runner is resolved on the
target, keeps the existing eight-rank limit and receives a new absent native
output directory. Prepared POSCAR provenance is registered only after target
publication; source inode, attempt, staging and finalization records are never
imported. Runtime, queue, storage, profile and destination working-directory
identity are checked again through the existing local admission gate.

## Explicit controls and outcome lookup

Selected-node `hold`, `release`, `move`, `cancel`, `suspend`, `continue`, `start`,
`stop`, `pause` and `resume` use the existing local services on that node. Append
`--preview` to inspect the consequence and bound identity without changing state.
Cancel and Suspend/Continue require `--yes` for node-selected CLI commands.
Suspension remains limited to attempts already enrolled through an existing
supported suspension contract; node enrollment does not qualify a native runner.

```console
cmw jobs --nodes-config FRONTEND_CONFIG --node mini release J1.1 --preview
cmw jobs --nodes-config FRONTEND_CONFIG --node mini release J1.1
cmw jobs --nodes-config FRONTEND_CONFIG --node mini start
cmw jobs --nodes-config FRONTEND_CONFIG --node mini cancel J1.1 --yes
```

The preview binds the node/queue/attempt, current boot clock and monotonic
control-relevant job/queue revisions. The frontend records that exact intent
before sending it. Same-ID retries return the original durable receipt; changed
semantics conflict. Relevant state transitions invalidate stale previews even
when the state later returns to its old value. Heartbeats and telemetry do not
consume confirmations. A reconnect never refreshes an old confirmation's clock
or revisions. A supplied `--request-id` remains bound to its original preview;
if a later preview differs, it refuses. Resolve an uncertain control with
`request REQUEST_ID --json` on that same node and queue before issuing new intent.

`ACCEPTED` records intent; it does not prove worker completion. Controller start
records its intent before the detached launch, then reports launch-unconfirmed.
Inspect status to establish that startup occurred. If the process fails between
commit and launch, looking up or retrying that request does not spawn another
controller; inspect the queue and issue a new explicit request with a fresh
preview if appropriate. Stop pauses dispatch and stops the controller while
existing detached workers continue. The endpoint never signals scientific PIDs
from the frontend.

## Portal and selected replicas

```console
cmw jobs --nodes-config FRONTEND_CONFIG watch --all-nodes
cmw jobs --nodes-config FRONTEND_CONFIG --node mini --queue research watch
```

`--all-nodes` observes the default queue of each explicitly enrolled connection.
Use `--node` and `--queue` for another enrolled named queue. It grants no
cross-node control authority. The compact portal preserves the existing
Scientific formatter and adds node identity to rows and selection. Each node
refreshes independently; cached disconnected rows say Last reported and retain
receipt-age qualifications. CPU, RAM, guards and external observations remain
per-node. History and Sources use the selected endpoint, while Copy uses the
frontend clipboard. Closing the portal closes observers only.

Files displays owning-node declarations, Copy Location and explicit Fetch. An
ordinary local folder opens only through a local connection and revalidated
location identity. A remote path never invokes the frontend folder opener by
spelling alone. Optional share mapping/opening is deliberately unavailable until
its endpoint/share identity can be established; no SMB setup or mount is required.

Fetch selects individual declared artifacts, not a recursive project tree:

```json
{
  "schema": 1,
  "storage": "scratch",
  "purpose": "summaries",
  "capture": "stable",
  "large_files": false,
  "files": [
    {"location_id": "REPLACE_WITH_EXACT_FILES_LOCATION_ID", "relative": "result.txt", "path": "result.txt"}
  ]
}
```

`relative` is an explicit child of a declared directory, or empty for a declared
file. `path` is the new replica-relative destination. Obtain the location ID
from `files --json`. The portal Fetch dialog accepts this local JSON document and
a new local destination; the equivalent CLI is:

```console
cmw jobs --nodes-config FRONTEND_CONFIG --node mini fetch J1.1 --spec fetch.json --destination /absolute/new/replica
```

Presets are semantic labels: `summaries`, `analysis`, `archive` or `restart`.
Normal captures are bounded to 128 files, 64 MiB per file and 256 MiB total.
Explicit large-file permission for archive/restart captures permits at most
2 GiB per file and 4 GiB total.
Stable/archive/restart captures require a completed attempt and stable selected
source versions. Active sources require `capture: observation` and always retain
`INCOMPLETE_OBSERVATIONAL` qualification. A stable capture is a `STABLE_REPLICA`,
not an accepted scientific result or a relocated original.

Origin verification and export capture occur on the execution node. A separate
SFTP channel transfers immutable capture bytes; the frontend verifies their
manifest and publishes into a new destination without overwriting. The
`fetch-manifest.json` records node/queue/attempt, source evidence, digest and
replica qualification. It conveys no source inode, process, staging or
original-location finalization authority. Adoption and finalizer relocation are
outside this MVP. Export evidence is retained for inspection and recovery.

The frontend records capture and local promotion identity before publication.
A lost completion acknowledgment reports `FETCHED` with
`acknowledgment_pending: true`; it does not erase the verified local result.
Retry the same `--request-id`, specification and destination to verify the owned
replica and acknowledge it without copying or overwriting. Fetch capture records
are separate from the Jobs registration/control `request` lookup. An existing
unrelated or copied directory is never silently adopted.
