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

## Selected-owner diagnosis

`cmw doctor --node ALIAS --queue QUEUE [--job JOB]` uses the explicit
`doctor.inspect` schema-1 operation. It checks the selected host/runtime/queue
without reading unrelated profile payloads or storage. Older endpoints refuse
this operation. Endpoint reads also scope checks to the selected owner/runtime/queue;
execution retains its required profile, storage and input checks. See
[doctor and private capsule contract](cmw-doctor.md) for runtime distinctions,
cached evidence, selected-job Details access and export limitations.

## Reviewed held submission

Lost-reply recovery requires the original durable request receipt and its exact
immutable attempt binding. A missing receipt for an already bound request, or a
corrupt receipt, refuses explicitly; matching job names never reconstruct it.

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
target, enforces the explicitly enrolled rank limit and receives a new absent native
output directory. Prepared POSCAR provenance is registered only after target
publication; source inode, attempt, staging and finalization records are never
imported. Runtime, queue, storage, profile and destination working-directory
identity are checked again through the existing local admission gate.

### Explicit VASP ranks up to 16

VASP profiles may explicitly enroll `max_cpus: 16` and `max_ranks: 16`;
submission resources may request `cpus: 16`, `mpi_ranks: 16`,
`threads_per_rank: 1`. Omitted VASP `max_ranks` still defaults to min(8, max_cpus).
Existing eight-rank profiles and queue records are unchanged and refuse a
16-rank request. Ranks cannot exceed profile CPUs or the ordinary maximum 16.

Preview checks the actual target runner's shared 16-rank guard in both planner
and MPI launcher. Old eight-rank or unrecognized runner implementations refuse
expanded requests. The current port's exact host-toolchain wrapper is recognized
without execution; helper, defaults and any local override are pinned dependencies.
A newly appearing unpinned override also refuses readiness. No automatic
re-enrollment or schema migration is performed.

Use valid NCORE/KPAR divisibility (for example 4/1 at 16 ranks), verified
runtime/storage/queue identities, available resources and compatible scientific
inputs. The physical M5 Pro qualification covers native Open MPI 5.0.11 / PRRTE
4.1.0, harmless 16-rank cleanup and one retained Si2 PBE VASP 6.6.1 fixture.
CMW preview and held registration were separately checked against that runner
source; no real calculation was dispatched through a research queue.
Controlled VASP suspension remains limited to eight ranks. GPU acceleration is
separate and is not established by this work.

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

The preview binds the node/queue/attempt, the host's kernel boot session UUID
and monotonic control-relevant job/queue revisions. A wall-clock adjustment
preserves that binding. A reboot invalidates it, and an unreadable boot
session refuses the operation. The frontend records that exact intent
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
refreshes independently. Its first connection failure marks retained observations
**Last reported** throughout the portal, including open and covered dialogs.
The timestamp records when the portal received the displayed data. Its age uses
the portal's monotonic clock. Owner commands are disabled while their source
view is stale. Recovery requires a compatible handshake, fresh status and a new
observation for that view. Reopen a frozen report or single-use confirmation
to obtain a new observation. CPU, RAM, guards and external observations remain
per-node. History and Sources use the selected endpoint, while Copy uses the
frontend clipboard. Closing the portal closes observers only.

The smallest supported portal is **77 columns by 24 rows**. A retained
observation shows **Last reported** in the existing frame or status line. A
record without a receipt shows **Last reported · receipt unknown** and cannot
authorize a command. The full caption includes the node alias, UTC receipt time
and monotonic age. At 77 columns ordinary aliases such as `mbp_m3max` fit with
all three. With less room, the formatter shortens the descriptive title first,
then drops the timestamp, then the age, and finally shortens the alias. The
full marker fits at the supported minimum. A caption temporarily narrower
than 13 cells uses `~` for last reported. The frame reserves the same number of
job-table rows in all receipt states. In a multi-owner search each affected owner has its own receipt
in the coverage summary.

Optional CPU average temperature uses the same node-local Jobs provider and
`machine_usage.thermal` contract. Each endpoint explicitly reads only
`<enrolled-config-root>/thermal.env` when creating its observer. For example:

```text
CMW_THERMAL_MACMON=/Applications/macmon/0.10.0/macmon
CMW_THERMAL_INTERVAL_SECONDS=20
```

This optional file and its canonical parent directory must be owner-only (0600
and 0700 respectively). It is limited to 4096 bytes, blank/comment lines and the
two unique literal settings above; values are unquoted and never evaluated by a
shell. A missing file disables temperature. Invalid settings produce a thermal
Details/JSON diagnostic without changing node readiness or CPU/RAM availability.
Closing and reopening the endpoint rereads the file and resets only that observer.
CMW never writes the file or enrolls a node through this monitoring route.

Ambient `CMW_THERMAL_*` variables are deliberately ignored by node endpoints,
including local endpoint connections. The frontend's optional user settings file
is not an endpoint configuration. The SSH bootstrap neither forwards these values
nor requires an interactive shell startup file. The future mini must opt in using
its own private file, provider installation and qualification; the MacBook's
provider path cannot authorize it. Enrollment, SSH configuration and
runner-profile dependencies are unchanged. An older peer with no thermal record
or an unsupported provider remains operational under its existing readiness rules.
The additive status record does not relax scientific-model decoding.

CPU/memory sampling belongs to the endpoint, independent of status requests.
The endpoint starts a bounded sampler when status is first requested, then samples
at 2 s intervals. Status returns the latest sample with its age; it still reads
current queue/external activity for admission. Samples older than 5 s are stale;
after 15 s their numeric usage is unavailable. Stopping or failing the sampler
marks evidence stale immediately; reopening starts with cold CPU baselines.
Slow samples do not overlap or create catch-up bursts. Cached samples retain
attempt/owner and external-membership bindings; repeated status polls do not
manufacture sustained sharing evidence. Temperature keeps its own existing policy.

For remote CPU and memory readings, the client uses the node's sample age plus
elapsed time on the receiving machine. It verifies that the sample stamp belongs
to the boot identified by the endpoint. Older records without a comparable
stamp, and readings without a local receipt, remain visible as stale with
unknown age. The client never compares monotonic readings from different hosts.

Each node retains independent CPU/RAM/temperature observations, connection state
and receipt age. At compact widths the portal gives concise node summaries and
the selected node's full shared line:

```text
Machine: CPU 71%; avg 63°C RAM 27.1 / 36.0 GiB
```

Wide views can show both node readings. Details/scheduling and Copy retain host
source, timestamp and freshness separately from scientific evidence. Thermal age
starts with the source age and conservatively includes transport/receipt elapsed
time; receipt never makes an old sample young. A disconnected node says Last
reported, qualifies cached CPU/RAM and temperature, then hides expired temperature.
Reconnection obtains new owning-node telemetry. Node/queue/session identities and
selection generations still prevent delayed replies from changing another target.

The [optional thermal contract](cmw-jobs.md#optional-average-cpu-temperature)
defines the 20-second default (15–30 configurable), 30/60-second freshness limits,
provider failure/cleanup bounds and pending physical hardware qualification.
There is no thermal service, remote monitoring server, database migration or
thermal admission/control policy. Isolated endpoint/loopback SSH acceptance does
not qualify physical dual-Mac sensors.

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

## Reviewed ORCA and Multiwfn routes

The molecular submission schema is advertised separately as
`molecular_submission: {schema: 1, engines: [ORCA, Multiwfn],
target_local_artifacts: 1}`. An older endpoint refuses this route explicitly;
changing a generic Command label is not engine support.

Enroll one target-local profile per foreground runner. ORCA profiles select the
packaged `orca/run_orca.sh` or `workflows/run_opt_freq_sp.sh`; Multiwfn profiles
select the matching packaged cube workflow or `multiwfn/run_ifct.sh`. The profile
`runtime` object uses the same recorded/synthetic engine runtime contract as local
Jobs. All executable, settings and runtime-record paths belong to the target;
clients cannot override them in a submission. Runner assets, their shared shell
closure and runtime files are pinned. Multiwfn retains the existing Bash wrapper.
Configured profiles do not establish real-engine qualification or suspension.
Installed executable/dependency identity is streamed with a separate 2 GiB per-file
bound, caching only a matching filesystem epoch. Portable input transfer remains
limited to 64 MiB per file and 256 MiB total. An empty selected-file bundle is
permitted when all scientific inputs are authoritative target-local artifacts.

Use the existing node selection syntax:

```sh
cmw jobs --node mini --queue research check --spec molecular-submit.json --json
cmw jobs --node mini --queue research submit --spec molecular-submit.json --preview --json
cmw jobs --node mini --queue research submit --spec molecular-submit.json --json
cmw jobs --node mini --queue research release J2.1 --preview --json
cmw jobs --node mini --queue research release J2.1 --json
cmw jobs --node mini --queue research start --preview --json
cmw jobs --node mini --queue research start --json
cmw jobs --node mini --queue research progress J2.1 --json
cmw jobs --node mini --queue research progress J2.1 --history --json
cmw jobs --node mini --queue research files J2.1 --json
```

`check` hashes only explicitly selected local inputs and contacts the selected
endpoint without creating a spool or staging inputs. Pretransfer scientific-input
checks are **UNASSESSED** because the endpoint cannot read files that have not been
transferred. Runtime pins and target-local donor authority are checked separately. A route with
only target-local donors and no portable inputs can run the full shared checker
immediately, without staging or writing anything.
`submit --preview` may create the established private client spool but never
stages remote inputs or registers a job. Full shared engine preflight runs on the
published destination bytes before held registration; unknown or unsupported
inputs never become executable through this route. Registration prepares the
exact scientific attempt and source locations. Release and dispatch remain
separate explicit actions. Actual worker launch revalidates pinned dependencies
and target-local donors; advisory dashboard polls do not hash large donors.

Molecular portable specifications use the ordinary node/queue/storage/profile,
request, resources, selected `files`, and `intent: "held"` fields. Replace generic
`arguments` with `scientific`. ORCA inputs use typed references, for example:

```json
"scientific": {"profile": "sp", "input": {"bundle": "inputs/job.inp"}}
```

Relative external XYZ dependencies must be selected in the same input tree.
Workflow `structure` and `config` use the same typed bundle references. The shared
ORCA checker retains the bounded dialect, resources, runtime and stage policies.

Multiwfn `source_result` must reference a validated original result on the chosen
node. It cannot be a transferred manifest. Its donor Jobs record must bind the
original ORCA stage result and scientific target through the molecular plan or
the recognized installed ORCA stage runner's explicit metadata and target
arguments. An arbitrary manifest in a generic Command working directory is not
source authority. A supported existing GBW-to-Molden result must additionally
prove its conversion-plan and original-result lineage. Compound workflow summaries
are not direct wavefunction sources. Select an already declared Files location
of that terminal managed donor attempt, then bind its exact path and digest:

```json
"source_result": {"artifact": {
  "target": {"node_id": "<node UUID hex>", "queue_id": "<queue UUID hex>",
             "job_id": 1, "attempt_id": "<Jobs attempt UUID hex>"},
  "location_id": "<Files location SHA-256>",
  "relative": "result.json", "role": "working-directory",
  "node_path": "/target/local/original/result.json", "sha256": "<file SHA-256>",
  "scientific_target_id": "<original scientific target SHA-256>",
  "scientific_attempt_id": "attempt_001"
}}
```

These are schema placeholders, not a runnable example. The endpoint resolves the
location from its own immutable Jobs identity and compares the supplied path;
the path is never MacBook filesystem authority. It checks enrolled storage,
original result/attempt identity, actual bytes, geometry and wavefunction lineage
using the existing source validator. A terminal Jobs status alone is not scientific
acceptance. IFCT `orca_output` is another exact artifact reference, and its checker
requires the source manifest's output. IGMH fragment/configuration files use
explicit bundle references. The donor's wavefunction remains on the target and is
not downloaded/re-uploaded. Donors on another host require a separately supported
import-lineage contract; this route refuses cross-host adoption and does not
rewrite result manifests.

Native logs and result records are available through the existing Files and
selected `fetch --spec ... --destination ...` path. Fetch replicas retain original
node/attempt provenance and never acquire original-result authority. Live
observation, process completion and scientific validation remain separate. No
physical MacBook-to-mini or real ORCA/Multiwfn qualification is implied by isolated
endpoint or loopback SSH acceptance.

## Runtime metadata and operation authority

New enrollment retains an optional original runtime_observation in its private
configuration. Existing configurations remain readable; missing original
object/security evidence never grants metadata-recovery authority. Semantic
runtime fingerprints and durable request digests are unchanged. Older endpoints
may refuse this new optional configuration field; retain their original enrollment
for rollback or peer checks rather than silently stripping the trust record.

Observation schema 3 separates original implementation content, filesystem
execution authority and raw observation evidence. Independent qualified Darwin
UF_TRACKED transitions on regular package members are verified without restarting
a package scan or spending a global recovery allowance. Unexplained ctime,
unknown/mixed flags, content, member/object/security changes and incomplete evidence
remain refusals. Root, directories and interpreter remain strict. Bounds are 4,096
files, 8,192 entries, 16 MiB/member, 128 MiB/package, two reads/member, 256 MiB read
bytes and a checked ten-second observation deadline.

The [policy decision and safety argument](runtime-observation-policy.md) defines
native ACL evidence, loaded-source assumptions, finite member verification and
legacy compatibility. Isolated coherent source imports remain required for metadata
revalidation; positively verified observation-only changes do not imply a loaded-code
mismatch solely through ctime. A material/uncertain refusal taints that verifier.
Schema 1/2 enrollments retain exact-match behavior only and are never silently
upgraded; metadata revalidation requires separately authorized enrollment. Semantic
content fingerprints and original request digests retain their existing domains.

Status now performs one full runtime verification per request when complete
schema-3 original security evidence is available, then compares every member,
directory, security attribute and interpreter/dependency identity at the response
boundary. It does not reread package contents at that boundary. Missing, changed
or incomplete evidence causes sticky `RUNTIME_MISMATCH`; no cross-request TTL or
hardware cache grants trust. Legacy evidence retains full verification at both
boundaries, as do all mutations. Owner/configuration/queue/boot checks still run
at both boundaries. This is code capability, not an enrollment migration.

Pre-existing compiled bytecode is accepted on an exact enrolled source match,
as before. Both status collectors inventory the same cache files and directories
alongside source members. A full request scan reads cache content into private
evidence; the response compares membership, object epochs and security metadata
without another content read. Any cache change refuses the response. `-B`
prevents endpoint writes but still permits existing bytecode reads. This is a
request-stability guarantee, not a new enrollment pin for bytecode; semantic
fingerprints and the stricter source-only rule for metadata recovery are unchanged.
For a sticky cache-only refusal, preserve the named paths and follow the
[bytecode cache recovery walkthrough](runtime-observation-policy.md#bytecode-cache-recovery).

Owner-side code derives operation dependencies, never a client skip list.
Selected reads check current host/runtime/queue before and after each call. Files
checks implicated storage roots; transfer/fetch retain their exact volume and
publication checks. Submission/final launch check selected profile/storage.
Release/Continue revalidate bound execution inputs outside the SQLite write
transaction, then existing revisions and ownership govern intent. Cancel/Hold/
pause do not depend on unrelated archive health. Legacy unbound controls retain
their behavior. Study/successor services still conservatively check aggregate
configuration readiness as well as exact source/input/owner checks; dependency
narrowing for those routes is not claimed.

Runtime errors preserve the actual object, observed comparison operands, generation,
content-read progress and original enrollment. Eligible raw transitions have explicit
ctime/flags differences and no material difference; completed content and security
verification is still required. Later diagnostics are not failure-time evidence.

Submission/transfer runtime refusals retain original request/node/queue identity,
before/after-dispatch context and explicit same-request lookup guidance in the
existing bounded error code/message. A rejected post-dispatch response is unconfirmed
until an independent fresh verified lookup; it is not a Jobs Unknown status or proof
that nothing was submitted. No automatic replay, trust rewrite or release is added.

### Navigation qualification diagnostics

Navigation return preserves the exact saved selection and viewport when content
and table/card geometry are unchanged. A new input cancels older restoration;
late scientific data, row changes or resize preserve the immutable target and
anchor with only necessary bounds/visibility clamping. A different visible-row
count after a real table-height change is not an exact-geometry comparison.

The focused portal regression gates scientific response and return callbacks in
both orders. `CMW_NAVIGATION_TRACE_DIR` retains full structured event/geometry
traces; failures always retain a counterexample path. Native PTY preflight uses
`tests.campaign.pty_barrier_smoke`, real keys/mouse reports, and resulting-screen
refresh barriers that abandon detached predecessors. The prospective visual
inventory is `tests.campaign.visual_contract_v2.plan()`: 24 core and eight modal
settled views plus transition/failure evidence. Its machine header is explicitly
synthetic; historical missing frames and failed gates remain unchanged.

## Runner interpreter profile records

New enrollment specs may explicitly set a profile's `schema` to `2` and
`runner_interpreter` to an absolute executable path. The stored pin includes
resolved path, file identity, SHA-256 and complete security evidence; a changed
pin refuses that profile's readiness. Such configurations use node schema 2.
Existing schema-1 configurations and unversioned profiles stay valid and report
**unpinned**. There is no automatic migration or rejection for a missing legacy
pin. `nodes check` exposes pin readiness; capabilities retain the old `profiles`
shape and add versioned `runner_profiles` interpreter metadata. A capability's
`pinned` label describes the record; selected-profile readiness verifies it.

This change adds data and observation only. Runner argv, environment and launch
selection remain unchanged. P3a will set `CMW_MANAGED_PYTHON` explicitly from the
profile pin; this Phase 1 record alone does not prove launch use.

## Portal reachability and recovery

Status polling is every 5 s. A receipt remains fresh through 15 s inclusive,
measured only by the portal monotonic clock; older or future receipts cannot
authorize controls. Stale/disconnected rows are labelled last reported. Unknown
Jobs states keep their existing display and refusal semantics.

Lane expiry, polling, handshake and reconnect continue while the portal is
running, even when a screen or one of its widgets is absent. Each view skips
only its unavailable presentation; restoring it renders the current lane state.
Closing the portal stops scheduling and closes its observation connections.

Each selected node has its own reachability state. Connection loss disables
controls immediately and qualifies retained rows as last reported. The first two
transport failures preserve the prior state; the third marks UNREACHABLE.
Authentication failure and incompatible capabilities are immediate states;
host-key, runtime, identity or readiness refusal immediately disables controls.
An unreachable/changed address is a transport failure; a presented key differing
from the pinned key is a security refusal, never an automatic pin update.

Recovery uses a new compatible capabilities handshake and fresh status. A reply
from before a newer refusal cannot restore controls. Unsent requests are abandoned;
mutations never replay. Unknown mutation outcomes are queried by their original
request ID on a separate verified connection. Transport failures retry after 5 s;
explicit refusals retry only a read-only handshake after 30 s. Authentication
classification requires complete local OpenSSH negotiation records, a verified
pin and exit status; arbitrary stderr text or exit 255 alone is insufficient.

The selected node displays signed clock skew and round-trip uncertainty. Absolute
skew of at least 5 s is amber and at least 60 s is red; a backwards frontend
wall-clock interval is unassessed. Skew is display-only and never grants freshness
or alters ordering, deadlines, endpoint checks or control authority.

The current MacBook-to-Mini route is direct SSH through `cmw-mini` to a literal
routable IPv6 address; there is no Tailscale. This code change does not configure
that route. Address stability, routable-SSH security and alias-based host-key
pinning need the pre-P3b review recorded in the architecture specification.
