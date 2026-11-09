# Job manifests: Phase 0 proposal

Privacy: node, queue and project names below are publication placeholders; the
private inventory retains their actual bindings. UUID fixtures are synthetic.

Status: design for [issue #41](https://github.com/angzeli/computational-modelling-workflow/issues/41),
based on main `66a372a82d5931372b324a7d23c4ff7ea72e8e86`. This PR adds schemas,
examples and schema tests only. Commands and protocols below are proposed, not
available interfaces. The separately requested collision inventory reads existing
queue/configuration records only; it does not install CMW, change production
state, access job input/output directories or installed runtimes, or restart services.

## Inventory before design

| Existing capability | Location, format and writer | Consequence |
| --- | --- | --- |
| Job creation and IDs | [`jobs/cli.py`](../../src/cmw/jobs/cli.py) calls `Store.add`; [`store.py`](../../src/cmw/jobs/store.py) uses `BEGIN IMMEDIATE`, `jobs.id INTEGER PRIMARY KEY AUTOINCREMENT`, `J{lastrowid}.1`, and a separate UUID `attempt_id` | Allocation is per **Jobs state database**, not per project or globally across nodes. Remote registration uses the same target-local Store. |
| Queue and cancellation | `<state>/queue.sqlite3`: JSON job/control payloads, events and `node_requests` receipts; Store, controller and original worker write their respective transitions | Already retains argv, cwd, env, resources, times, status, exit/signal, ownership, cancellation intent, node submission and optional layout/progress bindings. Reuse these facts. |
| Attempt lifecycle | `<state>/attempts/<UUID>/stdout.log`, `stderr.log`, `worker.lock`, `payload-exit.json`; controller locks/log at state root; runtime/payload helpers write them | Receipt `{exit_code, recorded_at}` alone does not establish completion. Default state is macOS `~/Library/Application Support/cmw/jobs`, otherwise XDG state; `--state`/`CMW_JOBS_STATE` may override it. |
| Operational completion | [`runtime.py`](../../src/cmw/jobs/runtime.py), `finish` and worker completion gate | `Done` requires exit 0 plus verified managed process-domain completion and original attempt/claim authority. Cancellation dominates; uncertain completion remains unresolved. None of this proves scientific convergence. |
| Scientific provenance | [`core/job.py`](../../src/cmw/core/job.py), [`provenance.py`](../../src/cmw/core/provenance.py), VASP preparation/finalization, ORCA job records | Target/attempt/geometry identities, input/artifact hashes and executable/resource provenance already exist in adapter-specific records. Generic Jobs does not freeze every input or output. ORCA already has an engine-specific `job.json`: do not overwrite it. |
| Human metadata | [`catalogue.py`](../../src/cmw/jobs/catalogue.py), catalogue relations, `Store.annotate` | Project/system/series/stage/tags, attempt relations and a revision-checked **replaceable** note exist. Append-only purpose/outcome events do not. |
| Node handshake | [`backend.py`](../../src/cmw/nodes/backend.py) capabilities, [`identity.py`](../../src/cmw/nodes/identity.py), [`runner_profiles.py`](../../src/cmw/nodes/runner_profiles.py) | Node/queue UUIDs, runtime fingerprint (package/Python/dependencies), runner/dependency identities, limits, readiness, interpreter pins and profiles exist. CMW version is not VASP build identity. |
| Bundle and binary evidence | [`transfer.py`](../../src/cmw/nodes/transfer.py), [`submission.py`](../../src/cmw/nodes/submission.py), [`backend_evidence.py`](../../src/cmw/periodic/vasp/backend_evidence.py) | Selected files already have SHA-256, size, role, mode and origin; registration binds bundle/submission/profile digests. Explicit POTCAR authorization is required. Upload currently refuses WAVECAR/CHGCAR (64 MiB/file, 256 MiB total), and managed VASP submission emits `--restart none`. Selected reviewed binary manifests can pin VASP executable/build evidence; general D22 VASP build advertisement is still planned. |

Relevant contracts are [Jobs](../cmw-jobs.md), [nodes](../cmw-dual-node.md),
[result evidence](../periodic/vasp-result-evidence.md),
[catalogue](../cmw-jobs-catalogue.md), [successors](../cmw-jobs-successors.md),
[publication](../periodic/preparation-publication.md), and the
[capability map](../../CAPABILITY_MAP.md). Geometry-only successors and typed
scientific artifacts are not already restart-file lineage.

Runtime safety is a deployment procedure, not an installer feature.
[D31](../architecture/multi-host-nodes.md#37-author-inputs-runner-hardware-and-builds-d17d22)
requires separately qualified immutable snapshots, pinned profiles/interpreters,
and retention of old snapshots. `runtime.detached` starts workers through its
own `sys.executable`. A separate snapshot does not retarget those workers.
There is no inspected CMW installer guaranteeing that arbitrary `pip install`
creates `~/.local/share/cmw/runtimes/<hash>/`; an in-place install could alter
the old environment. A directory label is not verified build identity.
The [dated deployment inventory](../architecture/deployed-runtime-parity.md)
also records historical Mini paths outside that namespace. This source audit
does not establish either node's current installed bytes or live job status.
**Phase 1 deployment prerequisite:** ordinary installation does not guarantee
that old workers retain isolated snapshot bytes. Qualify a separate immutable
snapshot and pinned interpreter/dependencies before any authorized deployment;
never replace the environment or source tree used by an existing worker.

## Layout and authority

Keep three per-job files because immutable provenance, execution observations
and human judgement have different owners and mutability:

```text
<project>/
  .cmw-project.json       # stable project key, owner UUID, frozen legacy node map
  .cmw/ids.sqlite3        # permanent allocations and allocation request receipts
  .cmw/integrity.jsonl    # ONE append-only integrity-receipt file for the project
  jobs/J28/
    .cmw-manifest/
      job.json           # immutable creation snapshot
      state.json         # executing-node projection, revisioned
      annotations.json   # append-only human event history, revisioned
    inputs/              # existing authored-input contract, unchanged bytes
    run/                 # existing runner work/output convention
```

Project-level coordination data is separate from the three per-job manifests.
`.cmw/ids.sqlite3` reuses existing SQLite transaction/request-id patterns for
allocation only; integrity anchors live in the single append-only project file,
not per-job receipt files or a rebuildable cache. This is not another scheduler.
The existing queue database remains operational authority. No PID, controller,
resource-reservation or scheduling state is reconstructed from these manifests.
`<project>` is the verified local project root on each participating node,
resolved through existing enrolled storage mappings; its absolute path may
differ across nodes. It is not a shared remotely writable queue or ledger.

New jobs reserve `.cmw-manifest/` at creation, before launch, and update state
there during execution. For **terminated legacy jobs only**, publish the same
three files into a new
`<existing-job-wrapper>/.cmw-manifest/`, adjacent to the existing run, after an
explicit reviewed locator mapping. Both use exactly one metadata structure:
`<job-wrapper>/.cmw-manifest/{job,state,annotations}.json`.
This avoids collisions with ORCA `job.json`
or any existing file. Never overwrite a collision, move a run, rename an ID,
change old input bytes, or rewrite recorded paths. Adapter provenance remains
at its current location; do not replace execution-layout records. Discovery
uses the reviewed mapping, not recursive guessing through calculation trees.
Running or unresolved legacy jobs, including `node-a:J27.1` and
`node-b:J4.1`, receive no sidecars until independently established terminal.

Files are the portable source of truth for the new manifest fields; queues and
adapter records retain their existing authority. Listing caches are disposable
projections. Files missing from a legacy job mean “not manifested”, not failure.

## Field reuse and definitions

All schema objects are closed, use `schema_version: 1` (JSON Schema 2020-12),
and reject unknown enum values. Null means unavailable evidence, never zero,
empty success, or a fabricated digest. Timestamps are UTC ISO 8601 with `Z`.
The schemas are [job](../../schemas/job.v1.json),
[state](../../schemas/state.v1.json) and
[annotations](../../schemas/annotations.v1.json), plus the project configuration
[schema](../../schemas/project.v1.json). The following tables account
for every proposed field, including nested fields.

| `job.json` field | Existing source / new meaning |
| --- | --- |
| `schema_version`, `provenance` | New generic contract; provenance is `native` or `backfilled`. Existing adapters have separate versions. |
| `id`, `project`, `id_owner_node` | New permanent project identity and allocation authority. Native IDs are flat; backfilled legacy IDs carry the frozen node qualifier described below. Catalogue project labels are human metadata, not this immutable namespace. |
| `created_at`, `authoring_node`, `target_node` | Reuse authoritative creation time and enrolled stable node UUIDs where available; new association in the portable record. Aliases are not identities. |
| `engine` | Reuse Jobs engine (`VASP`, `ORCA`, `Multiwfn`, `Command`). |
| `inputs.INCAR`, `.POSCAR`, `.KPOINTS` | Copy exact prepared/transfer input SHA-256 after identity verification, or compute once from stable authored inputs at creation. Generic add has only bounded optional POSCAR binding today. Non-VASP fields may be null. |
| `potcar[].element`, `.label`, `.family`, `.dataset_date`, `.sha256`, `.block_index` | Reuse ordered dataset identities from `selected_potentials`; preserve repeated species blocks and zero-based block indices. Unknown fields are null. No POTCAR content. |
| `preparation_record` | Optional existing record locator, not a copied parameter tree or parent identity. A locator alone is not an integrity assertion; verify the record's own existing contract before reuse. |
| `executable.path`, `.sha256`, `.version`, `.build_id`, `.variant` | Target-local locator and selected verified executable/build evidence from existing profile/binary records where available; general VASP handshake advertisement is future D22 work. Never substitute the runner shell or Python executable for VASP. Unknown build fields remain null. |
| `runtime_fingerprint` | Existing CMW runtime fingerprint, distinct from executable SHA/build. |
| `launch.argv`, `.mpi_ranks`, `.environment` | Reuse resolved target command and requested ranks from Store/submission. Copy only declared `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS`; never capture ambient secrets. Unknown environment is null, known empty is `{}`. |
| `parent`, `parent_inferred` | New restart-source reference: flat ID, node-qualified legacy ID, or either with a cross-project prefix, never a path. Bare dotted references are rejected. Explicit restart selection sets false; uniquely evidenced inference sets true. A null parent requires false. Existing geometry/artifact relations remain separate. |
| `restart_inputs.WAVECAR`, `.CHGCAR` | New byte identities of files actually copied in. Null for absent/unknown. Hashes prove byte lineage, not checkpoint validity, convergence or scientific compatibility. |

Prefer the existing POTCAR dataset identities above to the issue's raw element
title/TITEL proposal: this follows D21, avoids a second potential identity model,
and carries no licensed payload. Do not duplicate the full preparation spec,
node configuration, package dependency list or artifact manifest. A native VASP
job requires known input digests, creation time, nodes and launch argv; absent
future build metadata does not invent a value or silently choose a build.

| `state.json` field | Existing source / new meaning |
| --- | --- |
| `schema_version`, `id`, `project`, `provenance` | Version and exact join keys from job; provenance must agree across the triple. |
| `revision`, `writer_node` | New monotonic projection revision and executing-node authority; legacy writer may be unknown. |
| `binding.node_id`, `.queue_id`, `.job_id`, `.attempt_id` | Reuse target node/queue UUIDs, internal SQLite integer and immutable attempt UUID from the permanent registration receipt. These become known at target registration, so belong in mutable state, not author-frozen job.json. Null before registration or for unknown legacy binding. |
| `status`, `native_status` | Projection of the existing Jobs state, not a second lifecycle decision; mapping below. |
| `started_at`, `ended_at`, `exit_code` | Existing authoritative job times/exit. Do not infer terminal status from an exit receipt alone. |
| `cancel_mode` | `hard`, `soft`, `graceful`, or null; only the mode actually applied by a recorded supported cancellation operation. Current TERM/KILL evidence and future mode receipts are distinct. LABORT text alone does not establish who requested cancellation or a CMW mode. |
| `scf_result` | New persistent diagnostic classifier over existing bounded native source readers; definition below. Not policy acceptance. |
| `last_step`, `ionic_step`, `energy_ev`, `delta_energy_ev` | Reuse final matched electronic row and evaluation evidence; last_step is electronic index, ionic_step its enclosing ionic label. Energy is electronic-row E in eV and delta is electronic dE in eV, not ionic F/E0/d E. |
| `total_magnetisation_mu_b` | New bounded scalar whole-cell extraction, in Bohr magnetons; unavailable/vector evidence stays null. |
| `restart_outputs.WAVECAR`, `.CHGCAR`, `hashes_pending` | New stable terminal output byte digests, null until asynchronously computed. Hashing never blocks drainage or Done. A true pending flag means hashing is outstanding; null with false may mean absent/unavailable, never validated. |
| `evidence[].source`, `.marker`, `.line` | Bounded classification reason/line references, reusing source reader anchors. Source is OUTCAR/stdout/queue; `user-contract` is reserved for the explicitly unverified example expectations. Retain fuller source coverage/identity in existing evidence records rather than copy whole logs. |

The five issue statuses need one conservative extension: **null** for unresolved
or unavailable state. Preserve `native_status` alongside the projection:

| Native status | Manifest status |
| --- | --- |
| Queue, Hold, Starting | queued |
| Run, Cancelling | running |
| Done | done |
| Fail | failed |
| Cancelled | cancelled |
| Unknown, unavailable (`null`) | null |

Suspension is still governed by the existing operational record; `running`
does not claim CPU activity. Manifest state is not used to release resources or
unblock dispatch. A native `Unknown` must never become `done` via log parsing.

| `annotations.json` field | Existing source / new meaning |
| --- | --- |
| `schema_version`, `id`, `project`, `provenance`, `revision` | Shared identity plus append-only event count/revision, new generic representation. |
| `events[].sequence`, `.timestamp`, `.author_node` | New contiguous 1-based event order and attribution; known native events require UTC time and author identity. Wall clocks do not order events. |
| `events[].purpose`, `.system`, `.outcome`, `.note` | Human-authored changes only. Reuse catalogue system/note UI/service concepts; outcome is `reported`, `superseded`, `checkpoint-only`, `discarded`, or null (no change). New append-only history; never infer judgement from Done or a directory name. |

Each non-null event field updates that field's displayed value; null is “no
change”, not clearing. Empty strings can explicitly clear purpose/system/note;
outcome clearing is deferred. No-op events are rejected by the future command.
The current view folds events in sequence; history is never overwritten.
Do not automatically import the existing replaceable note as an historical
event with invented authorship/time. Legacy notes remain visible through their
existing interface until explicitly annotated/imported with truthful provenance.

Cross-file identity equality, writer authorization, event sequence/revision,
unique allocation, immutable history, path safety, digest comparisons and
atomic publication are semantic obligations beyond JSON Schema. The fixtures
test their available static invariants; concurrency/filesystem guarantees need
implementation tests in later phases.

## Allocation and restart lineage

`.cmw-project.json` at the project root has `schema_version: 1`, `project_key`,
`owner_node_id` (the enrolled node's 32-hex UUID, never an alias), and
`legacy_nodes` (frozen reference-token → node UUID map; empty without legacy
references), and `legacy_queues` (node token → queue token → initial queue binding).
Each node token has one UUID and each UUID one node token. Existing
`node-a`/`node-b` spellings become immutable reference tokens when recorded
here; later display-alias changes cannot retarget historical references.
These are new project bindings over existing node UUIDs. The schema checks
shape; enrollment identity and unique UUID values are semantic checks.

Each `legacy_queues` record reuses the existing enrollment fields `queue_id`
(nullable for unenrolled queues), `state_root`, `root_identity` and
`database_identity` (the initial device/inode pairs). Queue tokens are permanent
identity labels, not mutable queue aliases. Record each initial binding **once**
as a `queue_binding` event in the project integrity journal; configuration is
the materialized mapping, not a replacement authority. Never invent an enrollment
UUID for old queues. Their journal-bound token plus original path/filesystem
identity provides the stable reference. Multiple aliases for one database are
one queue; different original databases are distinct even when IDs coincide.

Paths/stat pairs are initial evidence, not the reference string. A later directory
move does not change `<node>/<queue>:<ID>` or rewrite its initial binding.
Resolve an authorized new locator through an appended `queue_locator` event
linked to the original binding, after identity verification (or explicit reviewed
cross-filesystem relocation evidence). Missing/unverified destinations refuse
lookup rather than rebinding automatically. This issue does not move directories
or authorize a relocation; the initial durable binding prevents a future move
from forcing ID changes. Schema validation cannot prove binding uniqueness,
locator continuity, or lookup ambiguity.

The owner is the node holding the project data. The accepted choice for
`example-project` is the node currently called **node-a**. Phase 0 does
not invent its UUID or create production configuration: resolve its enrolled
UUID when preparing the separately authorized Phase 1 setup. Example UUIDs
are explicitly synthetic. Ownership transfer is deferred.

1. The owner holds `.cmw/ids.sqlite3` on its local filesystem. All target queues
   for that project share this owner and verify the project key/owner UUID.
2. Before enabling allocation, inventory legacy IDs across **ALL in-scope nodes
   and queues for the project**, including retained deleted-job/tombstone records.
   Use exactly the collision-inventory scope below: persistent research and
   production-registered queues, excluding the locally inventoried archive/test
   databases (including disposable test enrollments). Apply the same reviewed
   project-membership filter; unrelated projects never inflate this counter.
   The first new ID is max(integer part of every legacy queue ID AND legacy label) + 1. Persist that
   high-water mark and thereafter allocate above all previous reservations.
   Gaps are intentional. Unreachable/uninventoried nodes or incomplete history
   block bootstrap; remaining directories alone cannot enforce never-reuse.
3. The author sends a durable request UUID plus normalized intent digest through
   the existing authenticated SSH JSON endpoint. A **new typed allocation
   operation** must be added in Phase 1; there is no current generic callback.
   No remote SQLite, remote filesystem lock, ad hoc shell counter, or new daemon.
4. On the owner, one `BEGIN IMMEDIATE` transaction serializes the counter,
   permanent reservation and request outcome. Commit durably before replying.
   Retries with identical UUID/digest return the same ID; changed intent conflicts.
   Short lock timeout returns a retryable busy error, never another allocator.
5. A lost reply is unresolved: query/retry that request on the same owner.
   Owner unreachable means creation fails closed. Failed publication,
   cancellation, deletion or an abandoned reservation burns the ID permanently.
   Retain the ledger/tombstones in backup; do not reset it from a listing cache.
6. Ownership transfer and automatic failover are out of scope. Do not edit the
   owner UUID to work around an unavailable owner or steal a lock based on wall
   time. Changed/conflicting ownership refuses allocation pending separate review.

**Legacy collisions are possible:** `Store.transaction` creates an independent
SQLite AUTOINCREMENT per state database, and `Store.add` formats that local
integer as `J<n>.1`. Both nodes can therefore contain `J4.1`; separate queues
on a single node can too. The read-only inventory below confirms actual collisions.
Manifest identities and parent references must retain origin:

| Reference | Syntax / example |
| --- | --- |
| Native own ID / same-project parent | `J28`, pattern `^J\d+$` |
| Unambiguous backfilled legacy own ID / same-project parent | `<node>:J<n>.<attempt>`; short form allowed only after complete in-scope resolution finds one queue |
| Queue-qualified legacy own ID / same-project parent | `<node>/<queue>:J<n>.<attempt>`, e.g. `node-a/project-queue:J27.1`; required when the short form is ambiguous |
| Cross-project native parent | `<project>:J28` |
| Cross-project legacy parent | `<project>:<node>[/<queue>]:J27.1`, under the same uniqueness rule |

The legacy suffix still matches `^J\d+\.\d+$`, and own qualified legacy IDs
require `provenance: backfilled` in every file. Bare `J27.1` is rejected in
manifest IDs and parent fields even if currently unique. Qualification does
not rename the existing queue display ID or directory. New allocated IDs are
canonical unpadded integers. Native jobs may reference qualified legacy parents.
Existing rename-id aliases outside these patterns need explicit mapping review;
do not silently rename or claim them supported by these schemas. JSON Schema
admits short and full syntax; the owner resolver must reject a short form with
zero/multiple matches, incomplete queue coverage or conflicting mappings. Full
form is always permitted and required for a collision. Resolve it through the
durable queue binding, retaining exact attempt evidence; duplicate display IDs
inside one database remain an explicit refusal. Do not guess from current queue
selection or human job names. Check short-form uniqueness across that node's
entire in-scope queue inventory before project-membership filtering; a project
label must not hide an existing node/ID collision. The counter remains strictly
project-scoped. Short/full spellings resolving to the same binding
and attempt are one job, not two allocations or parent candidates; materialize
the full form for known collisions. Sort numerically by integer component then
legacy suffix, node and queue tokens as tie-breaks; use
project/qualified-ID as identity, never purpose or catalogue system.

### Directory identity and human labels

For new jobs, the job directory basename equals the allocated job ID, always.
Human-readable names live only in annotations event `label` fields (nullable,
latest event is the current value); never use them in paths or as references.
This reuses the intent of existing queue names without importing their path
semantics. Legacy directories remain unchanged.

During terminated-job backfill, append every observed label-to-qualified-queue-ID
mapping as a `legacy_alias` event to the project integrity journal. The proposed
`legacy-alias.v1.json` schema describes that event, not a new per-job file.
Record labels even when identical to the queue ID; multiple labels may target
one ID and one label may have multiple targets. Preserve all mappings and refuse
ambiguous lookup. Labels are discovery aliases only: callers must select the
qualified queue ID before creating a parent or restart reference. Never substitute
a label into a reference merely because it matches the ID regex. Schema checks
syntax; the future resolver verifies actual queue identity and alias ambiguity.

Counter bootstrap reserves integer components from BOTH queue IDs and labels
matching `^J\d+(?:\.\d+)?$`, including retained historical names/tombstones.
Other custom numeric names require explicit accounting review (including M11.1),
never silent omission. Arbitrary human prose has no integer component. Unknown
label history blocks bootstrap; persist the reviewed maximum in the allocation
ledger. This prevents any new J<n> reusing a legacy numeric name.

`--restart-from <ID>` is an explicit byte-source choice. Verify stable source
and destination copies before freezing job.json; bind hashes of exactly the
copied WAVECAR/CHGCAR. With no explicit parent, infer only from a unique matching
set of known stable source hashes. Multiple matches, partial contradictory
matches or unknown sources leave parent null and report ambiguity. Do not
infer from paths, chronological proximity, method labels or filenames.
One parent means all copied restart files have that source; mixed parents are
refused/deferred rather than hidden. New non-null parents require at least one
known restart digest. Backfill can retain an independently evidenced historical
parent while a currently unavailable digest remains null, with that limitation
reported; the J27.1 contract fixture is not evidence of such a verified match.

Input restart files are hashed synchronously at submission, before immutable
publication; these hashes cannot be filled later into job.json. Output hashing
is deferred asynchronous work only after the original supervisor establishes
closure and files are stable. It never blocks process drainage, Done, terminal
publication or resource release. Digests remain null until computed, and failed
or interrupted work leaves null/pending evidence. Completion of the hash updates
state through its single writer. No hashing of J27.1 while WAVECAR is written.
Restart transport is a **separate multi-host requirement, outside #41**. Current
upload refusals and limits remain in place; do not smuggle files through support
roles or add transport work to any phase of this issue.
The current managed submission also fixes `--restart none`: metadata support
does not make even target-local restart execution supported. Phase 1 must
qualify a copy-only route using an existing compatible runner, without changing
scientific inputs or launch semantics. If that cannot be done, refuse that
submission and return the runtime integration question for separate review.
`--restart-from` never implicitly chooses INCAR restart settings.

## Single writers, synchronization and integrity

- **job.json:** one authoring operation owns the allocated ID. Resolve target
  profile/command before freezing; submit those exact bytes with the bundle.
  The target verifies acceptance against its current pinned profile and refuses
  drift rather than rewriting the file. Record its exact serialized SHA-256 in
  the project's append-only integrity file before acknowledging it.
  Target registration atomically binds the allocated project ID and manifest
  digest to the newly assigned queue job/attempt in its existing request receipt.
  Publish that binding in state and append its verified receipt to the same
  project integrity file;
  an interrupted acknowledgement is resolved using the same request, never by
  editing job.json or registering a second attempt. State readers verify the
  binding against that receipt. No unregistered state grants execution authority.
  Registration remains held until publication/receipt reconciliation succeeds;
  release and dispatch still require their existing explicit operations.
- **state.json:** one logical executing-node manifest publisher, integrated
  with the existing supervisor, owns writes. Queue-time initialization, worker
  updates and asynchronous hashes all pass through it, serialized by a local
  per-attempt lock with persisted revisions and attempt/claim fencing. A worker
  never competes with an independent parser/background writer. Before launch,
  publish queued state from the queue authority; no runner process is required.
- **annotations.json:** one project-owner append service accepts CLI requests
  from any node over the existing channel, uses expected revision plus durable
  request ID, and serializes appends under a local lock. Author node is the
  requesting node; the physical writer is the project owner. Offline editing
  is refused; there is no merge of independently edited histories.

Reuse `atomic_write_json` serialization/temp-file/fsync patterns for mutable
records, plus containing-directory fsync. Every runner metadata write uses a
temporary file **inside `.cmw-manifest/`**, then atomic rename there. The manifest
writer never writes, changes permissions on, moves or deletes any other file
in the job/run directory; scientific input/output behavior remains the existing
runner's separate contract. Immutable creation must use existing
no-clobber publication primitives instead of that helper's replacing behavior;
publish complete validated bytes and remove write bits before acceptance.
Preserve original bytes on every replica. No writable hard links to run inputs.
Readers see an old complete record or a new complete record, not partial JSON.

Sync is one-way per file owner. Receivers validate schema, project/ID/provenance,
node/attempt binding and revision; same revision with different bytes conflicts,
older revisions cannot overwrite newer ones. Disconnected replicas are explicitly
last received, never an execution authority. Transport receipt times and source
revision remain in existing transfer/observation receipts, not new wall-clock
ordering heuristics. Reuse selected-file publication, receipts and verified
owner connections; adding manifest transport is future protocol work, not
permission to bypass today's `inputs/`/`support/` allowlist.

**One project integrity file:** `.cmw/integrity.jsonl` is append-only and owned
by the project node, reached through the same authenticated channel as allocation
and annotations. Each complete JSON-line receipt records schema version, a
monotonic sequence, request UUID and event kind. `manifest`/`registration` events
carry project/qualified job ID, exact job.json SHA-256 and target queue/attempt
binding when known. `queue_binding` carries the initial project/node/queue token,
nullable queue UUID, state path and filesystem identities exactly once;
`queue_locator` links reviewed locator updates to that immutable binding.
Existing target request receipts remain operational evidence;
this file is the sole new durable manifest-integrity anchor, not one file per job.

Serialize append/readback under one owner-local lock, append the complete record
and newline, fsync (including the directory on first creation), then acknowledge.
Idempotency is keyed by `(event kind, request UUID)`: identical retries return
the retained receipt; conflicting contents refuse. A manifest anchor and its
later registration can therefore share a workflow request UUID without being
mistaken for conflicting retries. This is not an atomic SQLite-plus-file
transaction: hold publication/registration acceptance until both durable records
agree, and reconcile a lost reply using their request identities. Gaps burn IDs.
Never rewrite or truncate earlier receipt bytes, including an incomplete tail;
an incomplete/corrupt tail blocks further writes for explicit recovery review.
The valid anchored prefix remains inspectable with the damaged-tail finding.
An index may be rebuilt from complete verified receipts but cannot create a new
baseline from a modified job.json. Project backup must retain the entire file.

Integrity checking verifies schema, identities, read-only permissions and exact
job.json bytes against its **permanent receipt**; rechecks available parent/copy
hashes with explicit pending/missing/changed results. A read-only bit alone is
not tamper proof. The issue's “hash recorded in the index” must be changed:
a rebuildable cache cannot authenticate a hand-edited source. Cache rebuild
may not replace an integrity anchor; missing anchors mean unverifiable, not
accepted. This detects accidental edits relative to trusted owner records, not
a malicious actor controlling both records and backups. Running/unstable large
outputs are reported pending without being hashed by default.

`annotate` is **additive**: existing `annotate JOB --note` / `--clear-note`
and catalogue commands keep their current behavior for all jobs. They neither
append manifest events nor become projections automatically. The new explicit
form `annotate <reference> --manifest --project <key> --outcome ... --note ...`
appends through the project owner, using the same SSH channel as ID allocation.
Manifest notes/purpose/outcome are clearly labelled separately from the existing
replaceable queue note; no implicit dual write or migration. `jobs check
--spec` already performs molecular preflight: add an unambiguous manifest target
form (`check <ID>` / explicit project mode) without changing that existing form.
`jobs ls` should reuse catalogue/query adapters with filters system/status/
scf_result/outcome/parent and numerical ordering. This design does not silently
rename existing commands or remove their behavior.

## Scientific result classification

Use existing bounded `result_sources` readers, segmentation, electronic rows
and evidence anchors; add the classifier in Phase 2. Bind OUTCAR/stdout to one
attempt and one final invocation before using a marker. Multiple banners or
restarted invocations require explicit selection/binding; `Iteration i(j)`
defines evaluation boundaries. Never reuse convergence from an earlier
evaluation or an unmatched stdout stream. Preserve all independent evidence
even when the scalar result selects one cause.

| Value | Exact positive rule |
| --- | --- |
| `crashed` | Bound native text matches existing case-insensitive allowlist `VERY BAD NEWS`, `internal error`, `ERROR FEXCP`, `ZHEGV failed`, `ERROR EDD`, or `killed by signal`. Generic ERROR, nonzero exit alone, missing footer or elapsed time is insufficient. |
| `labort` | Final invocation contains OUTCAR `aborting loop because hard stop was set` or matched stdout `hard stop encountered! aborting job ...` as actual engine output, not an input echo. STOPCAR existence, `LABORT=.TRUE.`, `STOPCAR found`, generic soft-stop text and LSTOP are insufficient. |
| `converged` | Final electronic evaluation has `aborting loop because EDIFF is reached`, no later electronic iteration, no opposing native/numeric evidence, LABORT action or fatal event. Convergence at the NELM-th step is still convergence. |
| `nelm_reached` | Final evaluation says `EDIFF was not reached`, complete contiguous electronic history reaches effective positive NELM from the native parameter section, EDIFF is positive, and no higher-priority stop/fatal evidence exists. Count alone cannot classify it. |
| `unknown` | No positive rule, ambiguous source/invocation binding, relevant truncation, contradictory convergence evidence, unsupported grammar or EDIFF=0 fixed-count behavior. |

Precedence: unbound evidence → unknown; bound fatal → crashed; bound LABORT
action → labort; unresolved convergence contradiction → unknown; then converged,
nelm_reached, unknown. A later crash after LABORT is crashed. A later unbound or
incomplete invocation cannot inherit an earlier terminal marker. Positive
action/fatal evidence can survive an incomplete normal footer within the bound
invocation; missing data is otherwise not proof of failure.
Missing unrelated ionic geometry/finalization blocks do not erase fully bound
electronic facts; relevant truncation concerns the selected SCF evidence.

`last_step`, energy and dE come from the last complete matched DAV/RMM row,
with the enclosing ionic step retained. Do not replace electronic E/dE with
ionic-summary quantities. Existing numeric consistency checks retain both
energy differences; the single displayed dE cannot independently prove EDIFF.
Scalar magnetisation uses the final relevant OUTCAR
`number of electron <number> magnetization <number>` line; exclude augmentation
part and atom-projected sums. Noncollinear/vector or unbound data remains null.

The current result acceptance contract excludes HSE/hybrid calculations.
Reporting a recognized HSE termination fact is diagnostic, not an extension
of scientific acceptance or checkpoint reuse validity. The LABORT grammar and
magnetisation extraction are new; the existing broad stop regex is insufficient.
Qualify native dialects with authorized transcripts in Phase 2, not by reading
the still-running production jobs during Phase 0.

Marker basis: [existing parser](../../src/cmw/periodic/vasp/results.py),
[EDIFF](https://vasp.at/wiki/EDIFF), [OSZICAR](https://vasp.at/wiki/OSZICAR),
[STOPCAR](https://vasp.at/wiki/STOPCAR),
[ASE's maintained VASP reader](https://docs.ase-lib.org/_modules/ase/calculators/vasp/vasp.html),
[HSE LABORT transcript](https://vasp.at/forum/viewtopic.php?t=17264), and
[whole-cell magnetisation explanation](https://vasp.at/forum/viewtopic.php?t=19028).
VASP's LABORT “hard stop” wording is distinct from CMW signal-based hard cancel.

## Read-only collision inventory (9 November 2026)

The 12:36–12:38 UTC snapshot covered four persistent research/registered
production queues and 89 retained rows on both nodes. Both production queues
holding MacBook J27.1 and Mini J4.1 are included. There are 26 colliding IDs
across queues and no duplicate display IDs within an included database.
The full collision/review set, bindings, paths and individual exclusion reasons
are retained only in the local gitignored `.local/manifests-inventory.md`.

Excluded: 1,426 distinct database paths comprising 1,416 retained validation/
archive databases, five cutover backups and five registered disposable test
queues. Exclude these same categories from counter initialization; apply project
membership across all included nodes and queues. Synthetic test IDs and labels
never inflate project counters.

Discovery used saved registrations, standard state roots, one process-argument
snapshot per node and top-level project queue metadata. Nine databases were
copied by read-only file access; before/after identity/stat observations matched,
no WAL/rollback journal was present, and integrity checks passed on copies.
Archive discovery listed filenames only and pruned runtimes and scientific run,
input/output and attempt directories. Completeness covers the searched metadata
roots, not arbitrary orphan databases. Unknown queues/incomplete history block
bootstrap and short-reference resolution until reviewed. No runtime or scientific
run file was accessed and no production record was changed.

MacBook J27.1 is ambiguous and requires the queue-qualified fixture ID.
The historical label J10.7 identifies queue job J16.1; it is not a queue ID.
The fixture therefore references `node-a/project-queue:J16.1`.
Mini J4.1 is unambiguous within its node inventory. These are saved-record
findings, not verification of terminal state or hash lineage. The explicit
review set also includes the custom M11.1 identity and initial bindings of
three UUID-less MacBook queues. Record bindings once in the project journal;
reviewed later locator events preserve identity through moves.

The previously suggested J28 is provisional: bootstrap must also inventory all
legacy labels and tombstones under the same scope before choosing any starting ID.


## Backfill and examples

Proposed `jobs backfill <project>` is dry-run by default; only `--apply` after
review creates new metadata. Take an explicit source/locator inventory and
produce a field-by-field evidence/missing/conflict report. Apply only to closed,
stable jobs with no unresolved owner; active jobs are deferred. Revalidate the
reviewed sources before no-clobber publication. Existing metadata, inputs,
logs, permissions, paths and queue records are never rewritten by backfill.

Copy only objective source-backed facts. Prefer original creation/start/end
records over filesystem modification times; mtime is not a creation timestamp.
Never reconstruct the original input from a potentially mutated current file
without evidence that it was the executed input. Unknowns remain null. Parse
SCF diagnostics from bound native records; keep status from operational evidence.
Set provenance backfilled; record uniquely evidenced parent inference as above.
Start annotations with an empty event list; purpose/outcome require a human.

The J27.1 fixture is the **expected eventual backfill contract requested in
#41**, not a live completion observation: `status: done`, `scf_result: labort`,
parent `node-a/project-queue:J16.1`, `parent_inferred: true`, and own ID
`node-a/project-queue:J27.1`. The inventory identifies scientific run
label `J10.7` as queue job `J16.1`; this corrects the reference namespace without
changing the intended scientific parent. It does not verify restart-file lineage.
Its unavailable hashes and facts use null
placeholders. No hashes were computed from a run directory. Phase 4 may emit
that result only after independently establishing closure and the source facts;
current LABORT/WAVECAR-writing context is not terminal evidence.

Examples and negative cases are under [tests/fixtures/manifests](../../tests/fixtures/manifests/).
Draft 2020-12 tests validate all instances offline, validate the schemas and
reject invalid IDs, native dotted IDs, unknown outcomes and missing versions.
Synthetic marker/expected-state cases specify the Phase 2 parser contract;
schema validation does not execute or qualify an OUTCAR parser. Future tests
must additionally prove same-project two-node allocation, unique short-form
resolution versus required queue qualification, durable legacy queue binding,
lost-reply recovery,
never-reuse, stale state rejection, append idempotency, no-clobber backfill and
hand-edit detection against retained anchors.

The validator is a test-only `schema-tests` optional dependency, enabled in the
existing CI test installation; it does not add a CMW runtime dependency or a
new workflow. For local validation, install `jsonschema[format]>=4.18,<5` in a
disposable environment and run its interpreter from the checkout with
`PYTHONDONTWRITEBYTECODE=1 python -m unittest -v tests.test_job_manifest_schemas`.
No CMW installation or runtime import is needed for these tests.

## Phases and safe deployment

Each phase is a separate reviewed PR; none is deployment authorization. Do not
modify/reinstall an old runtime, restart a service, enroll an active queue,
switch its profiles, collect live restart hashes, or adopt its worker.

| Phase | Change and gate |
| --- | --- |
| 0 (this PR) | Design, schemas, fixtures and schema tests only. Stop for design decisions; no runtime behavior, packaging release or production mutation. |
| 1 | Extend existing creation/submission with root project identity, project ledger/SSH allocation, uniform `.cmw-manifest/` layout, project integrity file, manifest binding, restart selection and immutable publication. Preserve queue UUID/internal IDs and legacy routes. Test all-node bootstrap, concurrent allocation, qualified legacy IDs and crash/lost-reply burns. Same-node restart selection requires stable verified copies; restart transport remains a separate multi-host requirement outside #41. Bind available build evidence; coordinate general advertisement with D22. Runtime isolation qualification below is a deployment prerequisite. |
| 2 | Add the executing-node state publisher and bounded classifier, synthetic plus authorized native parser fixtures, stable asynchronous output hashing. Reuse supervisor completion; never make SCF parsing a completion gate or signal policy. Existing workers receive no new writer. |
| 3 | Add explicit manifest forms to annotate/query/check, enforce owner writes via the allocation channel, project-receipt verification and append-only events. Add the agent rule forbidding direct manifest edits, with schema-test fixtures explicitly distinguished. Existing commands keep their behavior. |
| 4 | Implement reviewed dry-run/apply backfill; do not automatically run it on `example-project`. A separate authorized window must establish closure, stable evidence, safe destinations and parent identities. Report unresolved jobs/nulls; leave active runs untouched. |
| 5 (deferred) | Views are not planned for implementation under the accepted scope; revisit only on a separate explicit request. No view directory or symlink is created by earlier phases. |

For later deployment, first qualify source/synthetic changes in an isolated
test environment. During a separately authorized maintenance window, establish
both nodes' actual runtime/runner provenance and complete the existing deployed
parity/D31/D33 gates. Build a **new** versioned immutable runtime snapshot,
verify it, then enroll only eligible quiescent queues with explicitly reviewed
profile/endpoint pins. Keep old snapshots/configurations intact for their jobs
and rollback. Merely changing the frontend must not grant old endpoints new
mutation authority; negotiate capabilities and fail closed on old versions.

J27.1 on node-a and J4.1 on node-b remain under their original runtime,
worker, queue and configuration until independently confirmed terminal with no
unresolved ownership. A persistent Unknown is not a drained queue. If safe
cutover cannot be established, postpone deployment. Rollback does not downgrade
new queue state into an old runtime; isolate new manifest-enabled work and keep
its compatible runtime until it too is closed. No phase automatically launches,
cancels, restarts or moves a calculation.

## Accepted decisions and remaining setup gates

The project-root identity file and data-owning node, all-node legacy maximum,
optionally queue-qualified legacy references (required when ambiguous),
synchronous input hashing, nonblocking asynchronous
output hashing, nullable unresolved status, one append-only project integrity
file, uniform sidecars and owner-routed additive annotations are accepted.
Terminated-only applies to legacy backfill; new jobs create metadata before
launch and update it atomically during execution, confined to `.cmw-manifest/`.
Ownership transfer, manual adoption and views are deferred. Restart transport
belongs to a separate multi-host requirement, not an extension of #41.

Later setup still needs the actual enrolled owner/node UUIDs, a complete all-node
legacy inventory (including same-node queue ambiguity), and qualified immutable
runtime isolation before Phase 1 deployment. None requires accessing production
in this Phase 0 PR. General build advertisement follows existing D22 work.
These decisions do not authorize Phase 1 implementation, deployment or backfill.
