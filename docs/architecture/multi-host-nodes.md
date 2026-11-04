# Multi-host CMW nodes: delta specification

Status: **Phase 0 contracts; runtime changes are future work.** This specification
extends the existing `cmw nodes` implementation. It introduces no second jobs
protocol, scheduler, manifest, provenance format, transport, or portal. The pure
APIs described below are not imported by a CLI, endpoint operation, or portal.

The inspected baseline is cached `origin/main` commit `1563c2f`, which includes
PR #3 and the existing nodes implementation. The local `main` reference was
older and lacked `src/cmw/nodes`. The earlier `phase0-multi-host-contracts`
commit `4c9f9b9` and its uncommitted follow-up were reference material only; that
branch was neither merged, rebased nor removed. Existing architecture documents
remain in place. [The implemented nodes contract](../cmw-dual-node.md) and
[Jobs contract](../cmw-jobs.md) describe current supported behaviour.

## 1. Baseline

This section records code behaviour, not physical two-machine qualification.
The proposed aliases do not establish that either production enrollment or a
reverse connection has been configured or tested.

| Area | Existing implementation and boundary |
| --- | --- |
| Identities and enrollment | [`identity.enroll`, `load_node`, `check_node`](../../src/cmw/nodes/identity.py) preserve distinct node, queue, host and storage identities. Aliases select records; they are not durable identity. Hardware evidence is the platform UUID on macOS, obtained with `ioreg`, rather than a hostname. Runtime identity pins package/interpreter/dependency evidence and typed runner profiles. [`Store.enroll_queue`](../../src/cmw/jobs/store.py) uses schema-4 enrolled queues and refuses unsafe active-state enrollment; observation does not migrate queues. |
| Registry and storage | [`connections.default_config`, `read_connections`, `validate_connection`](../../src/cmw/nodes/connections.py) already support the default `~/.config/cmw/nodes/connections.json`, private ownership, schema 1, at most eight local or SSH connections, explicit default queues and pinned UUIDs/runtime/host keys. A registry may currently contain multiple queues per node. [`storage.py`](../../src/cmw/nodes/storage.py) verifies enrolled volume/mount/root evidence; a same-named replacement directory does not acquire storage authority. |
| Endpoint and framing | [`protocol.py`](../../src/cmw/nodes/protocol.py) uses integer `PROTOCOL = 1`, UUID request IDs, strict bounded JSON-lines, a 2 MiB frame maximum, and `{code, message}` errors in uppercase style. Requests contain `protocol`, `request_id`, `operation`, `target`, `params`; replies contain `protocol`, `request_id`, `ok` and either `result` or `error`. [`endpoint.serve`](../../src/cmw/nodes/endpoint.py) retains one backend for up to 4096 frames, flushes each reply and closes only its observers on exit. |
| Backends and sessions | [`LocalBackend`](../../src/cmw/nodes/backend.py) owns queue reads, checks identity/runtime before and after each call, and returns schema-1 observations with node/queue IDs, observer session, sequence, `observed_at`, `clock_id`, host/runtime and result. [`ConnectedBackend`](../../src/cmw/nodes/connections.py) uses local calls or [`SSHTransport` / `ProcessTransport`](../../src/cmw/nodes/transport.py), with one in-flight request, capabilities before mutation, pinned SSH keys, no PTY/agent forwarding and bounded diagnostics. The SSH endpoint is a persistent stream. No remote SQLite is opened. |
| Observation and polling | [`NodesApp.tick`, `accept`, `NodeLane.fresh`](../../src/cmw/nodes/portal.py) poll status every 2 s, use a 10 s inclusive freshness window, immediately expose a first transport error and back off after failures. Observation sequences are per session, not durable journal sequences. [`observed_machine_usage`](../../src/cmw/nodes/connections.py) combines node-local evidence age with frontend monotonic receipt age; it already avoids subtracting unrelated node/frontend clocks. |
| Jobs, admission and resources | [`Store`](../../src/cmw/jobs/store.py) owns durable attempts, revisions and request receipts. States include `Hold`, `Queue`, `Starting`, `Run`, `Cancelling`, `Unknown`, `Done`, `Fail`, `Cancelled`. Existing resource declarations include CPUs, memory, MPI ranks and threads per rank. [`activity.project`](../../src/cmw/jobs/activity.py), admission and sharing retain conservative commitments and external-activity evidence; low observed usage is not spare-resource authority. Nodes reuse these services rather than scheduling across hosts. |
| Submission | [`client.py`](../../src/cmw/nodes/client.py), [`submission.normalize_spec`, `preview`, `register_held`, `launch_blocker`](../../src/cmw/nodes/submission.py) prepare a private frontend spool/request record even for preview, publish verified selected inputs and register only `Hold`. Registration and its durable request receipt share a transaction. Request lookup and digest checks handle lost replies. Target-local storage, runner, input and receipt checks run again at launch. Current VASP argv fixes `--binary std`, `--restart none` and explicit NCORE/KPAR runner flags; typed ORCA/Multiwfn routes also exist. `Command` is currently accepted through the remote route. |
| Controls and cancellation | [`control.py`](../../src/cmw/nodes/control.py) provides revision/boot-bound previews and request-receipted hold, release, move, cancel, suspend, continue, dispatch and controller controls. [`Store._change`](../../src/cmw/jobs/store.py) sets `cancel_requested` / `Cancelling` for active cancellation. [`runtime.worker`, `finish`](../../src/cmw/jobs/runtime.py) signal only verified owned process groups, use a 2 s TERM-to-KILL grace, confirm drainage, and retain `Unknown` when ownership is uncertain. Cancellation has no mode or STOPCAR history yet. A cancellation request prevents later normal exit from becoming `Done`. |
| Upload and fetch | [`transfer.py`](../../src/cmw/nodes/transfer.py) already validates explicit selected-file manifests, private stable spools, hashes, request conflicts, disk space, target-owned staging and no-clobber publication. Upload uses SFTP remotely and copying locally. Inputs are limited to 128 files, 64 MiB each, 256 MiB total; POTCAR requires its role and explicit authorization; restart upload is refused. [`fetch.py`](../../src/cmw/nodes/fetch.py) binds selected owner/attempt locations, captures stable or observation versions, verifies receipts and publishes a new local replica without overwriting. Explicit stable archive/restart fetch currently has larger fixed limits (2 GiB/file, 4 GiB total). Retrying a pending completion acknowledgement does not recopy an already verified replica. |
| Hashes and input identity | [`canonical_json_bytes`, `stable_hash`](../../src/cmw/core/provenance.py), `transfer.manifest_digest` and `submission.submission_digest` already provide canonical normalized identity and idempotency. Their existing encodings and record digests remain unchanged. [`parse_incar`](../../src/cmw/periodic/vasp/inputs.py) supplies parsed settings/findings including NPAR. [`potentials.py`](../../src/cmw/periodic/vasp/potentials.py) records element, label, family, dataset date, dataset SHA-256, ordered block index and whole-POTCAR SHA-256; it does not require raw TITEL. |
| Thermal and scientific progress | [`thermal.configured_provider`](../../src/cmw/nodes/thermal.py) reads only private endpoint-local `thermal.env`; optional macmon sensor availability and age are already separate from CPU/RAM and admission. [`jobs.telemetry`](../../src/cmw/jobs/telemetry.py), [`jobs.progress`](../../src/cmw/jobs/progress.py), location resolvers and the shared [`progress.service`](../../src/cmw/progress/service.py) already support owner-bound observations, including the managed VASP `stdout.log` source. No new progress reader or thermal backend is required here. |
| Portal and file UI | [`jobs_cli.watch`](../../src/cmw/nodes/jobs_cli.py) already opens [`NodesApp`](../../src/cmw/nodes/portal.py) for explicit node selection or `--all-nodes`, including registered local backends. Ordinary [`jobs.cli`](../../src/cmw/jobs/cli.py) still opens [`JobsApp`](../../src/cmw/jobs/tui.py). `NodesApp` shares Jobs CSS, prompts, details, scientific display and navigation helpers, but `JobsTable` and `NodesTable` remain separate. It has per-owner monitoring and confirmed controls, catalogue filtering and explicit fetch; automatic local inclusion, node tabs and fetch-and-open are not implemented. Remote paths are not interpreted as local paths; share opening is refused. |

The existing source therefore already supplies local/remote symmetry at the
backend boundary, persistent transport, observations, held submission, controls,
verified fetch and most display components. Deployment in both directions,
production identities, consolidated entry points and the deltas below still
require their named phases.

## 2. Decisions

The following table records the task's normative decisions, including the
follow-up decisions for D16 and D29.

| ID | Decision |
|---|---|
| D01 | Keep existing node, queue and storage UUIDs as durable identities. Aliases become `mbp_m3max` and `mm_m5pro`. |
| D02 | Each machine has exactly one node enrollment, one queue (alias `main`) and one storage (alias `scratch`, outside TCC-protected folders). Each machine has one default registry at `~/.config/cmw/nodes/connections.json` listing every node, including itself. |
| D03 | Mini reuses node `702779636fc14335bcf71c1395549f43` and queue `2b975d09e4104933b7c1c146538fbc6b`, which hold research jobs that must be preserved; only aliases change. MacBook is enrolled fresh as `mbp_m3max`. Test enrollments are archived after confirming they hold nothing to keep. |
| D04 | Keep the existing integer protocol plus `capabilities`. No MAJOR.MINOR scheme. |
| D05 | Keep the persistent SSH endpoint stream and host-key pinning; add a forced-command gate that dispatches by whitelist to the endpoint or to the binary transfer endpoint. It ships only after D06. |
| D06 | All file transfer, upload and fetch, moves to one dedicated binary transfer endpoint replacing SFTP: 16 MiB chunks, byte-offset resumption, per-chunk and whole-file SHA-256, content-addressed deduplication on the target, background QoS, disk-space preflight. Reuse the existing stable-capture, hashing, receipt and non-overwriting publication logic. |
| D07 | Input files keep the existing limits (128 files, 64 MiB per file, 256 MiB total) and explicit `potcar_authorized`. Explicitly declared restart artifacts (WAVECAR, CHGCAR) are bounded only by the disk-space preflight. |
| D08 | Keep Hold → release. The portal presents one flow with two confirmations. Release is impossible until every declared restart artifact has been transferred and verified. |
| D09 | Accept the existing preview's spool and request-record writes. |
| D10 | `UNREACHABLE` after 3 consecutive transport failures; authentication failure and incompatibility are immediate. |
| D11 | Poll every 5 s; staleness threshold about 15 s. |
| D12 | Reduce the cost of each `status` call; runtime verification must not re-hash the package twice per call. |
| D13 | Cancel modes: `graceful` (STOPCAR `LSTOP = .TRUE.`, after the current ionic step), `soft` (STOPCAR `LABORT = .TRUE.`, after the current electronic step), `hard` (immediate). Escalation only `graceful → soft → hard`; repeating the same mode is a no-op; downgrades are rejected; `graceful` has no timeout. ORCA supports only `hard`. A cancelled job is never a completed result. |
| D14 | Hard cancel waits 10 s between SIGTERM and SIGKILL (existing: 2 s). |
| D15 | Keep the existing terminal state spelling `Cancelled`. |
| D16 | `--mode` is required with no default. |
| D17 | Parallelisation is decided by the author. INCAR is executed byte-for-byte unchanged. MPI ranks and threads are author-supplied. |
| D18 | The VASP runner must verify, not rewrite, NCORE/KPAR/NPAR: runner-profile values must equal the INCAR's effective values or submission fails. This change lives in the VASP port repository, not here. |
| D19 | Plan checks: absent KPAR/NCORE take VASP defaults of 1; NPAR overrides NCORE; ranks × threads above physical cores is an error; above `recommended_max_parallelism` is a warning (skipped when null); an incomplete plan blocks submission. |
| D20 | Native core tiers are recorded verbatim and uninterpreted; `recommended_max_parallelism` is node policy, nullable. |
| D21 | POTCAR is prepared on the authoring node and transferred; use the existing POTCAR identity set (no raw TITEL line). |
| D22 | Capabilities advertise VASP build information; submission names the executable variant; experimental builds require explicit selection. |
| D23 | The generic `Command` profile accepts only locally originated submissions. Remote submissions may use only typed profiles. New software gets a new typed profile. |
| D25 | Clock skew: amber at ≥ 5 s, red at ≥ 60 s; no logic depends on clocks agreeing. |
| D26 | A node-wide permanent journal with node identity and durable per-node sequence numbers (Phase 4). |
| D29 | On battery, `mbp_m3max` accepts submissions into Hold but refuses release and dispatch. While jobs run, the controller holds a `caffeinate` assertion against idle sleep. This cannot prevent lid-close sleep on battery. |

## 3. Deltas

Except for the explicitly identified pure Phase 0 APIs, everything in this
section is a **future specification**, not implemented behaviour. D01–D29 above
are normative. New operation names, capability keys, wire layouts and additional
record fields below are **draft specification choices** where the decisions did
not prescribe them; they require implementation review, versioned negotiation
and tests before use. Existing strict schema-1 readers must never receive extra
fields they cannot accept. Missing future capabilities fail closed for the new
feature and preserve existing supported operations.

### 3.1 Enrollment, aliases and protocol: D01–D05

Preserve node/queue/storage UUIDs, immutable job and attempt IDs, owner bindings,
request receipts and existing state roots. Adopt node display aliases
`mbp_m3max` / `mm_m5pro`, queue alias `main`, storage alias `scratch` and the one
registry per machine in D02. Each registry includes its own node through the
local backend and the other node through the existing SSH backend. Paths remain
local enrollment data; aliases are never substituted into scientific identity.

P1 must inventory existing records before any alias or enrollment work. The
Mini node `702779636fc14335bcf71c1395549f43` and queue
`2b975d09e4104933b7c1c146538fbc6b` are retained with their research attempts and
receipts. The Mini storage UUID must be read from that enrollment and preserved,
not invented here. Alias migration must keep UUID-bound selections valid, update
references atomically or refuse, and leave a recovery record. Fresh MacBook
production enrollment and any archival of disposable enrollments are separate
P1 operations after preservation checks. P0 changes none of these files.

Retain protocol integer 1, JSON-lines control framing, the response/error shape,
capability negotiation, observer sessions and host-key pinning. Feature versions
belong in negotiated `capabilities`, not a new MAJOR.MINOR envelope or `hello`
protocol. Draft capability additions cover binary transfer, cancellation modes,
VASP build/variant selection and hardware plan evidence. Exact nested schemas
must be versioned because current validators are strict. Existing
`INCOMPATIBLE_PROTOCOL`, `UNSUPPORTED_OPERATION`, `IDENTITY_CHANGED`,
`QUEUE_CHANGED`, `RUNTIME_MISMATCH` and `OUTCOME_UNKNOWN` meanings are retained;
new uppercase codes below are proposed, not current wire promises.

D05 adds only the gate described in §3.3. It must not cut off SFTP until the
replacement transfer path is available and qualified.

### 3.2 Binary transfer, restart artifacts and receipts: D06–D09

#### Scope and authorization

One dedicated binary endpoint carries upload and fetch data. The existing JSON
endpoint continues to authorize selections, create/lookup durable transfer or
fetch records and publish/acknowledge receipts. It does not carry 16 MiB chunks
inside its 2 MiB JSON frame. Local and remote clients use the same transfer
semantics; local invocation need not use SSH. SFTP is removed from the supported
new transfer route only in P3b, after upload and fetch parity is established.

Keep the regular status/progress observer channel separate from transfer work.
The frontend opens a dedicated backend/SSH connection for potentially expensive
capture/hash/prepare/completion requests, and a separate gate-selected binary
connection for payload streaming. Reuse the existing explicit-operation/fresh
backend pattern rather than putting a long fetch preparation ahead of status on
the observer's single in-flight queue. Bind both transfer connections to the
same durable receipt and enrolled owner; a separate channel grants no extra
permission. Disk and CPU contention remain possible, so background QoS and
bounded work are still required; channel separation does not promise zero
interference with running calculations or observation latency.

Retain canonical path checks, selected regular files, case-fold collision checks,
no symlinks or directory recursion, private owned staging, forbidden credential
and engine files, and explicit `potcar_authorized`. Input/support/POTCAR limits
remain 128 files, 64 MiB per file and 256 MiB combined. A versioned manifest
extension declares WAVECAR and CHGCAR separately as restart artifacts with exact
role, relative destination, byte length, digest and source identity. They are
excluded from those input byte limits and have no new fixed file-size/total-byte
cap: disk-space preflight is the size bound. They never become implicit inputs
by a matching basename. Count policy and archive-only fetch limits remain open
in §5; do not silently apply the old 2/4 GiB limits to declared restart artifacts.

A draft transfer authorization binds direction, node/queue/storage UUIDs,
request/fetch ID, manifest digest, selected file identity, size/hash, allowed
operation and endpoint session. Server-owned private state resolves the real
paths. A client-supplied path, hash, alias or existing CAS object never grants
access by itself. Fetch retains the original immutable job/attempt binding and
selected location evidence. POTCAR bytes and other payloads never enter control
errors, diagnostic logs or journals.

#### Draft binary framing and resume handshake

The following is a draft subprotocol, not a second version of the control
protocol. Advertise a versioned binary-transfer capability with a 16 MiB chunk
size (`16 * 1024 * 1024` bytes). Use bounded control headers containing request,
file, byte offset, byte length and SHA-256; payloads are raw bytes. One proposed
frame is a 4-byte unsigned big-endian header length, a strict UTF-8 JSON header
(maximum 64 KiB), then exactly the declared payload length. Maximum data payload
is 16 MiB; every nonfinal chunk has that length. Empty files have no data frame
and still require whole-file verification. These header/framing numbers other
than the mandated chunk size must be finalized in P3b.

1. The client obtains or looks up the original control-plane transfer/fetch
   record. A draft `open` header provides its authorization, direction and
   immutable content descriptor; the receiver verifies enrolled owner, allowed
   file and storage, source capture identity, and disk reservation before data.
2. The receiver reports only a **verified durable byte offset**. Before offering
   a retained prefix, it re-reads and hashes that prefix and compares every
   complete chunk with the agreed chunk hashes for the same whole-file identity.
   Chunk descriptors may be exchanged in bounded pages rather than an unbounded
   manifest array. A trailing partial chunk is not acknowledged as complete.
3. The sender checks the offset is in range and chunk-aligned except for the
   completed final file. It confirms the same stable source capture and hashes
   the chunks it sends. A changed source or a differing manifest is a conflict,
   not a continuation of the old request.
4. For each chunk, the receiver checks expected offset, length and per-chunk
   SHA-256, writes only the owned partial object, and acknowledges the next
   durable offset after the data and resume metadata are durably recorded.
   Duplicate acknowledged chunks may be verified and acknowledged again; skipped
   offsets or different bytes at the same offset are refused.
5. If the retained prefix fails verification, return the failed byte boundary
   and refuse that claimed resume offset. Recovery may discard only the owned
   unverified suffix and resume from the last verified boundary; it must never
   trust a saved offset, file length or filename without hashing retained bytes.
6. Completion checks declared size and whole-file SHA-256 before publication,
   deduplication reuse or a completed receipt. Lost replies are resolved through
   the original request/fetch record. A durable verified object can satisfy a
   retry; uncertain transport delivery cannot imply publication or submission.

Draft errors include `INVALID_TRANSFER_FRAME`, `TRANSFER_OFFSET_MISMATCH`,
`CHUNK_HASH_MISMATCH`, `PREFIX_HASH_MISMATCH`, `SOURCE_CHANGED`,
`TRANSFER_INCOMPLETE` and `INSUFFICIENT_SPACE`. Reuse existing `REQUEST_CONFLICT`,
`INVALID_MANIFEST`, `TRANSFER_LIMIT`, `POTCAR_AUTHORIZATION_REQUIRED` and
`UNSAFE_TRANSFER_PATH` where their current meanings fit. Transport failures keep
`OUTCOME_UNKNOWN` whenever a mutating outcome may have occurred. No lower-case
parallel error vocabulary is introduced.

#### Target deduplication, cleanup, QoS and disk preflight

The receiving target owns a private content-addressed store under its enrolled
storage. For uploads this is the execution node; for fetched replicas it is the
receiving local storage. Address content by SHA-256 plus declared size and verify
actual bytes before reuse. Content equality does not confer source authority:
receipts still bind each authorized transfer to its original selected owner,
role and source capture. Publish objects exclusively and atomically. Working
inputs/outputs must not share a writable hard link with a supposedly immutable
CAS object; materialize by verified copy or qualified copy-on-write semantics.

Track references from active transfers, held submissions, published input
receipts and unfinished fetch receipts. Garbage collection operates only on
owned unreferenced CAS/partial objects, under locking and fresh reference checks;
it cannot delete original research inputs/outputs, published user replicas or
unknown historical records. Interrupted transfers retain verified prefixes for
resumption. Explicit abandonment removes only their owned partials and releases
reservations. P3b must settle retention intervals, reservation expiry and crash
reconciliation in §5 before enabling automated cleanup.

Preflight counts all additional physical allocations: source capture where
needed, unverified staging, final CAS storage, materialized publication/replica,
filesystem overhead and a policy reserve. Credit only already verified reusable
objects. Reserve capacity across concurrent transfers, recheck before each new
allocation/publication and refuse space loss without producing a complete
receipt. Space checks run on every filesystem involved, including the fetch
recipient. Sparse-file or CoW savings cannot be assumed. This is the only size
bound on explicitly declared restart artifacts.

Run transfer work at background CPU/I/O priority and bounded concurrency so that
calculation and status traffic remain usable. The platform mechanism, concurrency
limit and bandwidth policy are draft implementation details, not new scientific
resource decisions. The endpoint must report unsupported QoS explicitly instead
of claiming it applied; chunked I/O remains bounded. Cancellation of a transfer
stops that transfer only, never the calculation being observed.

#### Mapping onto existing receipts and the two-confirmation flow

Preserve `transfer.begin` / lookup / publish lifecycle and current semantic
request identity. Binary transport replaces the byte-moving SFTP/copy stage.
Only a fully verified input/restart set can produce the publication evidence
required by `register_held`; registration retains its existing same-transaction
job insertion and request receipt. Retrying the same request and identical
normalized content returns its existing result; changed content remains a
`REQUEST_CONFLICT`. No new parallel idempotency helper is needed.

Preserve `fetch.prepare` stable/observation capture, selected-source receipts,
verified no-clobber destination publication and `fetch.complete`. The new
endpoint consumes the same captured source version. Local publication still
precedes completion acknowledgement, so acknowledgement-only recovery cannot
recopy or overwrite a verified replica. Old schema-1 records/digests remain
readable and are not rewritten to acquire new fields. New fields use an
explicitly negotiated record version or linked record, with legacy pending SFTP
requests handled by the migration decision in §5.

The portal combines the existing stages into one flow with two confirmations:
first review the explicit target, author plan, files, transfer permission and
held registration; then review the exact registered attempt for release.
Frontend preview may continue writing its private spool and request record
(D09). Release stays separate from upload/registration. Enforce restart
completeness server-side at release **and** the existing launch blocker, not
merely in a disabled UI button. Every declared artifact must have size/hash and
publication evidence bound to the same attempt; missing, changed or incomplete
artifacts refuse release/launch. The stricter draft choice to complete all
transfers before creating Hold should be settled with the UX sequencing question
in §5; D08 itself only mandates the release barrier.

### 3.3 Forced-command gate and origin: D05, D23

The gate runs only a server-owned whitelist entry and never an arbitrary shell
command, client executable, config path, queue path, environment assignment or
SFTP subsystem. A draft private whitelist schema contains a `schema` integer
and entries with a selector, fixed absolute executable, canonical config root,
node UUID, queue UUID and queue alias, plus allowed services `control` and
`transfer`. Entry selection is an exact bounded token match. Selectors are
transport routing names, never replacement node identities.

The authorized-key command fixes the gate and whitelist. SSH daemon/key policy
must enforce `restrict,command="..."` or equivalent explicit restrictions
(`no-port-forwarding`, `no-agent-forwarding`, `no-X11-forwarding`, `no-pty`,
`no-user-rc`). The gate parser cannot itself prevent forwarding or PTY allocation;
those restrictions belong to sshd. A draft client request selects only service
and entry token (for example `cmw-node control mini-main`);
the gate parses an exact grammar with no shell expansion and maps to fixed argv.
It checks the current enrolled config/queue UUID pair matches its entry and the
expected private ownership/security before dispatch. Reject extra tokens,
unknown selectors/services, stale enrollments, symlinked or writable policy,
and attempted subsystem/shell command strings. The control service invokes
the existing persistent endpoint; the transfer service invokes the new binary
endpoint under the same enrolled owner/storage constraints. Neither path offers
an escape to unrestricted filesystem access.

D23 must be enforced in the endpoint's submission service as well as the gate.
The trusted transport construction supplies local-versus-remote origin; a JSON
`origin` string or source alias cannot assert locality. Remote direct endpoint
access, successor/study registration and any equivalent submission path must
also refuse generic `Command`; a whitelist routing decision alone is not enough.
Locally originated `Command` continues through existing local checks. Remote
submissions use typed profiles only, with a new typed adapter for new software.
Draft refusals are `REMOTE_PROFILE_FORBIDDEN`, `GATE_REFUSED` and existing
identity/profile errors. The origin policy is not retroactively applied to
already registered attempts or used to invalidate their historical receipts.

Ship the gate only after both binary upload and fetch are usable. P0 does not
install authorized keys, alter SSH settings, run a gate or change permissions.

### 3.4 Reachability, staleness and clocks: D10, D11, D25

The pure [`contracts.py`](../../src/cmw/nodes/contracts.py) adds
`Reachability`, `NodeState`, `NodeEvent`, `transition`, `classify_staleness`,
`classify_clock_skew` and `estimate_clock_skew`. It reads no clock or transport;
callers supply observations. `None` is an unconfirmed initial state, not an
additional wire node state. A compatible `capabilities` handshake is the event
that establishes or restores `REACHABLE`.

| Event | State and consecutive transport-failure count |
| --- | --- |
| Compatible handshake | `REACHABLE`; reset count to zero and record local monotonic receipt time. |
| Ordinary successful observation | Reset count and receipt time; preserve the current state. It cannot clear `AUTH_FAILED`, `INCOMPATIBLE`, `UNREACHABLE` or initial uncertainty without a new handshake. |
| Transport failure or timeout, count 1 or 2 | Preserve previous state and last successful observation; increment count. |
| Transport failure or timeout, count 3 or greater | Become/remain `UNREACHABLE`, except that explicit `AUTH_FAILED` or `INCOMPATIBLE` remains the more informative state. |
| Authentication failure | Immediately `AUTH_FAILED`; reset transport-failure count; retain last observation as historical. |
| Incompatible handshake | Immediately `INCOMPATIBLE`; reset transport-failure count; retain last observation as historical. |

Every prior state, including the unconfirmed state, follows this table. A
transport classification adapter must distinguish authentication, incompatible
capabilities, host identity refusal and outcome-unknown mutations; the current
SSH transport does not expose all of those as separate events yet. Host-key or
runtime mismatch is never three tolerable failures before mutation refusal.
Explicit security/readiness refusals disable controls immediately even while
reachability counts are below three. Recovery never silently replays a mutation.
Retry cadence/automatic authentication retry policy remains an integration
question, independent of this pure transition function.

P1 status polling becomes 5 s. The Phase 0 interpretation of “about 15 s” keeps
the existing inclusive freshness convention: age `<= 15 s` is fresh, `> 15 s`
is stale, no receipt is unavailable. Age is measured solely with the portal's
monotonic clock; no negative/future local age is accepted. Retained rows are
labelled as last reported when stale or disconnected. Freshness does not override
endpoint, ownership, revision or readiness checks for actions.

Skew is display-only: estimate `node_observed_at - (portal_sent +
portal_received) / 2`, with half the round-trip interval as uncertainty, from
wall-clock observations of the same request. Absolute skew below 5 s has no
warning, `>= 5 s` is amber, `>= 60 s` is red. Preserve the sign and uncertainty.
A backwards frontend wall-clock interval is unassessed/refused, not a fabricated
clock correction. Continue existing node-local aging and monotonic frontend
receipt aging for all decisions, deadlines and ordering. Never order permanent
journal events by comparing wall clocks from different nodes.

### 3.5 Cheap status and local measurement: D12

`LocalBackend.call` currently runs `_check` before and after dispatch.
`check_node` obtains hardware evidence and calls `RuntimeVerifier.verify` on each
check. `RuntimeVerifier` retains a trusted baseline and sticky refusal, but its
provider still performs `runtime_observed` and the complete bounded package
content verification every time. Thus retaining a verifier instance does not
avoid two full content scans per successful status call. `runtime_observed`
already performs initial/final inventory and security checks around each scan.
The status body separately uses queue reads, external activity and telemetry.

**Draft P1 design:** create one request-scoped verified runtime observation for
`status`, including its existing bounded content scan, interpreter/dependency
identity, complete member/directory/security evidence and loaded-code coherence
checks. Keep owner/configuration/queue/boot checks at both request boundaries.
At the response boundary, compare a complete cheap epoch/security/inventory and
interpreter/dependency observation with the verified request snapshot without
hashing package contents a second time. Check every member and directory, not
only the root mtime. New/deleted/replaced files, metadata/ACL/xattr changes,
changed interpreter/dependency evidence, incomplete evidence or changed identity
refuse the response and retain sticky runtime diagnostics; do not silently
accept cached hashes. A fresh isolated observer performs any permitted recovery.

This is request-local reuse, not indefinite trust after startup. The first
implementation keeps at least one full package verification per successful
status call. It must preserve existing schema-1/2 legacy refusal/projection
rules, schema-3 security evidence, source-import coherence, capability checking
and `RUNTIME_MISMATCH` recovery semantics. Mutation operations retain their
stronger pre/post verification and `OUTCOME_UNKNOWN` handling until a separately
qualified change. Tests must exercise mid-request replacement/security changes
and lost outcomes before enabling the optimization. If equivalent trust cannot
be established for a platform or evidence version, withhold that P1 rollout or
fail closed; continuing the old double-hash path is not completion of D12. Do
not silently weaken trust to meet a latency target. Exact dependency/epoch
factoring remains an implementation question.

Do not cache host identity merely because an alias matches. An immutable
session-attested hardware identity may reduce repeated `ioreg` cost only if the
boot/host/session validation and failure categories remain explicit; this is a
separate proposed optimization whose measured value and safety must be reviewed.

#### Local status-cost measurement

Measured on 2026-11-04 using only disposable MacBook node
`ba6411e78a594f14915cbfd069640939`, queue
`daa341b79c2c4ad295eb150a87a15281` (alias `disposable`, one existing job).
The measurement used its already enrolled immutable runtime, Python 3.14.3,
runtime fingerprint
`072fdbcaf73b097bf156cde846fcc557923ef8734e2a9fc9e19d7967b6763aea`,
in isolated/no-bytecode mode. It did not run the new branch as a replacement
endpoint or change the enrolled runtime.

Method: call the actual `endpoint.serve` with in-memory protocol-1 framed status
requests and its real persistent `LocalBackend`. Capture timing through response
flush. Send ten requests at a 5 s start-to-start cadence: the first session call
and nine warm calls. Use `perf_counter` for wall time, `process_time` for observer
CPU and `getrusage(RUSAGE_CHILDREN)` for reaped child CPU. Nested wrappers subtract
timed child stages, so each stage below is exclusive. The total includes framing
and small unassigned overhead. Time `_check`, `host_evidence`,
`runtime_observed`, `Store.queue_identity` / `snapshot`, `Observer.scan` and
`Sampler.sample`; the configuration/identity residual excludes the separately
listed hardware/runtime/queue stages. Instrumentation overhead is included and
was not separately calibrated. No test suite ran during sampling.

| Warm-call metric (seconds, nine calls) | Median | Minimum–maximum | Interquartile range |
| --- | ---: | ---: | ---: |
| End-to-end wall | 0.623609 | 0.603636–0.932085 | 0.606962–0.683854 |
| Observer CPU | 0.583230 | 0.566625–0.755402 | 0.570203–0.619464 |
| Child CPU | 0.036517 | 0.035353–0.046233 | 0.036437–0.041728 |
| Total CPU, summed within each call | 0.620961 | 0.603094–0.797713 | 0.606720–0.661192 |

| Exclusive stage | Calls per status | Median wall (s) | Wall minimum–maximum (s) | Median observer CPU (s) | Median child CPU (s) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Configuration/identity residual | 2 | 0.017539 | 0.011399–0.021857 | 0.017420 | 0 |
| Hardware identity, including `ioreg` | 2 | 0.044596 | 0.040166–0.056690 | 0.005183 | 0.036517 |
| Runtime verification | 2 | 0.534065 | 0.516123–0.791640 | 0.533292 | 0 |
| Queue reads | 3 | 0.001166 | 0.000936–0.003095 | 0.001165 | 0 |
| External activity | 1 | 0.026635 | 0.026210–0.061810 | 0.026657 | 0 |
| Telemetry | 1 | 0.001256 | 0.001160–0.004184 | 0.001421 | 0 |

The first-session call took 0.695860 s wall and 0.660724 s total CPU; interpreter
startup/import time before `serve` is outside that number. Warm wall observations
were 0.932085, 0.606444, 0.631955, 0.606962, 0.603636, 0.623609, 0.615867,
0.747578 and 0.683854 s. Quartiles use inclusive interpolation. Medians of stages
and CPU components need not add to the median total. Median unassigned wall time
was 0.001232 s. These samples identify duplicate runtime verification as the main
cost; they are not a physical dual-node or worst-case latency qualification.

All ten responses succeeded. External activity reported fresh `BUSY` evidence;
this was observation only and did not contact or control a scientific engine.
Memory telemetry remained fresh. Six of nine warm CPU observations reported
`warming-up`: the existing sampler requires successive samples no more than
`MAX_AGE = 5.0` s apart, so a 5 s poll interval plus scheduling/verification jitter
can invalidate its CPU baseline. The proposed P1 resolution in §5 separates
node-side telemetry sampling from status polling; changing the portal interval
alone would degrade CPU display.
No thermal provider was configured, so thermal collection cost was not measured.

Configuration and queue filesystem metadata (identity, mode, size and mtime)
matched before and after. The measurement did not alter enrollment, registry or
queue data, contact the Mini, use production/research queue state, launch an
engine, use SSH/network, or enable a provider. Only read-only timing wrappers
and the measurement output outside those roots were created.

### 3.6 Cancellation: D13–D16

[`contracts.py`](../../src/cmw/nodes/contracts.py) adds pure `CancelMode`,
`available_cancel_modes` and `decide_cancel`. VASP permits `graceful`, `soft`,
`hard`; ORCA permits `hard` only. Other engines have no new mode contract until
specified. The function requires an explicit argument. Under D16, the future
CLI requires `--mode` with no default; Phase 0 does not change the existing CLI.

| Current mode | Request graceful | Request soft | Request hard |
| --- | --- | --- | --- |
| None | Start graceful | Start soft | Start hard |
| graceful | No-op | Escalate to soft | Escalate to hard |
| soft | Reject downgrade | No-op | Escalate to hard |
| hard | Reject downgrade | Reject downgrade | No-op |

Apply engine availability first. Unsupported modes return
`UNSUPPORTED_OPERATION`; downgrades return `REQUEST_CONFLICT`; malformed mode
arguments raise `INVALID_PARAMS`. Repeats perform no new STOPCAR/signal side
effect and do not create another escalation. There is no graceful timeout or
automatic escalation. Hard cancellation begins immediately with verified
SIGTERM and waits 10 s, measured monotonically, before SIGKILL if the same owned
group still exists. “Immediate” does not remove D14's verified TERM-to-KILL
interval. Soft timing/automatic escalation beyond the selected mode is not
introduced by this specification.

For active attempts, extend existing `cancel_requested` / `Cancelling` with a
versioned cancellation record bound to node/queue/job/attempt, confirmed request
ID, effective mode and append-only mode/escalation history. History records the
requested mode, prior/effective mode, outcome, actor/origin evidence, node-local
sequence/time, STOPCAR ownership evidence or process-signal evidence, and cleanup
outcome. Effective mode never moves backwards. Request receipts preserve exact
idempotency after lost replies; no-op repeats may return the prior outcome
without manufacturing another escalation. Preview/revision checks must include
cancellation mode state so concurrent requests cannot overwrite an escalation.

Setting `cancel_requested` for graceful/soft must no longer drive the current
unconditional TERM path. Worker handling must consult the committed mode.
`Cancelling` retains resource commitments and dispatch policy until verified
process completion. Normal VASP exit after STOPCAR still finishes `Cancelled`;
scientific convergence or exit zero cannot turn it into a completed result.
Preserve uncertain ownership as `Unknown` and retain evidence. Existing
`Cancelled` spelling, finalization refusal and historical records stay valid.
Legacy active records with no mode must retain their established hard-cancel
meaning, through an explicit migration/compatibility rule rather than assuming
that a missing field was a graceful request. Pending/terminal and already
suspended attempt semantics require the review in §5.

STOPCAR handling is owned by the verified target attempt. Resolve its run
directory through recorded runner/location evidence, never a frontend cwd or
unverified PID. Create private regular files atomically: graceful writes
`LSTOP = .TRUE.`; soft writes `LABORT = .TRUE.`. Persist ownership tied to the
attempt, path identity, exact content and request before claiming success. On
escalation replace only the STOPCAR still matching that ownership evidence;
reject an unrelated existing file or later foreign edit as draft
`STOPCAR_CONFLICT`. Do not overwrite a user's STOPCAR based on its basename.

Remove only the attempt-owned, unchanged STOPCAR after verified termination, or
as part of a fully recorded safe transition that cannot remove another request's
file. Hard escalation must not remove a file before the intended cancellation
is secured; process termination remains ownership-verified. Crash recovery must
reconcile the record and actual file before any write/delete. Failed cleanup is
recorded and surfaced, not hidden by declaring scientific completion. The
precise durable write/history transaction and cleanup retry protocol are P3a
work, not a Phase 0 filesystem helper.

### 3.7 Author inputs, runner, hardware and builds: D17–D22

The pure [`vasp_plan.py`](../../src/cmw/nodes/vasp_plan.py) consumes the existing
`parse_incar` result, ordered POSCAR/POTCAR species evidence, explicitly supplied
MPI ranks/threads and target hardware facts/policy. It does no file reading,
input modification, runtime discovery, submission or POTCAR construction.
`VaspPlan.require_submission` rejects incomplete/error plans with
`INVALID_SUBMISSION`; a future caller must invoke that gate rather than treating
warnings or partial fields as permission.

A successfully parsed INCAR without KPAR or NCORE uses default 1. An unreadable,
unparsed, invalid or unsupported INCAR is incomplete and cannot use defaults.
Explicit NPAR takes precedence over NCORE, including a malformed NPAR value;
there is no fallback to a conflicting NCORE. Positive integer ranks/threads are
author inputs, not guessed from hardware. KPAR must divide ranks (error).
Nondivisibility of the effective NCORE or NPAR split within a KPAR group is a
warning retained from the reference pure plan; exact runner acceptance is still
the D18 gate. Missing species evidence makes the plan incomplete; a different
POSCAR/POTCAR species order is an error. Missing ranks, threads or hardware also
blocks a complete plan; malformed explicit values are errors rather than defaults.

`Hardware` contains `physical_cores`, `core_tiers` and
`recommended_max_parallelism`. Each `CoreTier` records native `level`, `name`
and `physical_cores`; names are preserved verbatim and do not imply performance
or efficiency classes. An empty tier list is allowed when no tier detail is
available; total physical-core evidence is still required. `ranks * threads`
above physical cores is an error; above a non-null policy recommendation is a
warning. A null recommendation suppresses only that warning. No tier count,
name, CPU utilization or logical CPU count supplies a hidden policy default.
Strict JSON parsing reuses the existing protocol codec; no new wire schema is
introduced in P0. Future collection and capability advertisement remain separate
from this supplied-data model.

**D18 interface requirement on the separate VASP port PR:** the runner advertises
verify-only parallelization support, accepts author ranks/threads and an expected
effective KPAR plus the effective NCORE-or-NPAR selection, and compares those
values with the exact staged INCAR before launch. Absent-tag defaults and NPAR
precedence match D19. Any mismatch fails submission/preflight; it must not
rewrite, remove or append INCAR tags. Preserve byte-for-byte author INCAR through
staging and execution, with the existing stable byte identity checks. The
CMW-side profile must select a qualified port interface; an older rewriting
runner is refused for this new contract. Exact flags/capability spelling are a
draft interface, not instructions to patch the port here. NCORE equality when
NPAR makes it ineffective and current preparation incompatibilities remain in §5.

D21 retains the current ordered dataset identity set and whole-file digest.
Prepare POTCAR on the authoring node, transfer only with explicit authorization,
and verify identical bytes and species evidence on the target. The target does
not regenerate from its local potential library. Do not introduce raw TITEL
requirements or log POTCAR bytes; synthetic test evidence is sufficient for P0.

For D22, extend capabilities with versioned VASP build records: executable
variant, build identity/provenance, availability and experimental status. A
submission names its exact variant and binds the selected advertised build in
its reviewed plan. No implicit fallback to `std` or an experimental build is
allowed when the named variant is unavailable. Existing profile/runner byte
pins remain authoritative. Proposed refusals include `UNAVAILABLE_VARIANT`,
`EXPERIMENTAL_SELECTION_REQUIRED` and `RUNNER_PARALLELISM_MISMATCH`; use existing
`INVALID_SUBMISSION` for the current pure gate. Build descriptor schema,
experimental selection representation and the timing of hardware advertisement
are future capability review items, not Phase 0 endpoint changes.

### 3.8 Permanent journal and portal consolidation: D26 and phase boundaries

P4 adds a permanent node-wide journal with durable per-node sequence numbers,
node identity and event/request/attempt bindings. Existing observer-session
sequences, queue event rows and request receipts remain intact; they are not
renamed into a journal or ordered across nodes by wall clock. Journal append,
receipt/state atomicity, crash recovery, retention/export and initial sequence
allocation need a versioned record design. No journal writes happen in P0.

P2 changes ordinary `cmw jobs watch` to the existing `NodesApp` with the enrolled
local queue included, adds node tabs/filtering and consolidates the existing
Jobs/Nodes table widgets. Preserve shared scientific views and owner-qualified
selection/confirmation rather than building a third portal. P5 reuses fetch
receipts for explicit fetch-and-open and replaces hand-written selection JSON
with file-selection UI; P6 polishes that UI. Remote source paths remain remote
until a verified local replica exists.

D29 permits `mbp_m3max` to accept submissions into Hold on battery, but refuses
release and dispatch while on battery. While jobs run, the controller holds a
`caffeinate` assertion against idle sleep. This cannot prevent lid-close sleep
on battery. This is a future runtime policy; Phase 0 adds no power checks or
sleep assertions.

## 4. Phase plan

| Phase | Content | Prerequisite |
|---|---|---|
| P0 | This delta specification; ported pure logic; local status-cost measurement | — |
| 3.11 PR | Minimum Python 3.11; CI adjustment | P0 merged |
| P1 | Cheap status; 3-failure rule and 5 s polling; default registry and aliases; production enrollment of both machines in both directions; thermal configuration | 3.11 PR merged |
| P2 | Single entry point: `cmw jobs watch` opens `NodesApp` including the local queue; node tabs; consolidate `JobsTable` and `NodesTable` | P1 |
| P3a | Cancellation modes and 10 s hard grace; VASP port runner verify-only INCAR (separate repository PR) | P2 |
| P3b | Binary transfer endpoint replacing SFTP; restart-artifact submission; forced-command gate; D23 | P3a; gate after the transfer endpoint |
| P4 | Node-wide permanent journal | P3b |
| P5 | File UX: fetch-and-open, selection without hand-written JSON | P3b |
| P6 | UI polish | P5 |

## 5. Open questions and implementation conflicts

These are recorded for the responsible phase. They do not authorize changing
normative decisions or wiring Phase 0 contracts into runtime entry points.

1. **D01–D03 enrollment migration (P1):** current aliases can appear in connection,
   storage and submission records, while UUIDs are durable. Determine the exact
   atomic rename/reference-compatibility procedure from real enrolled records;
   do not reenroll the Mini queue or discard its receipts. Confirm its existing
   storage UUID/path and preservation inventory, and independently qualify the
   newly enrolled MacBook and both registry directions. No such inspection or
   change is part of P0's disposable-node measurement.
2. **D05/D06/D07 transfer rollout (P3b):** finalize binary framing, bounded chunk
   descriptor exchange, capability/record versions, token/session binding,
   reservation recovery, CAS reference/retention policy and QoS implementation.
   Decide how in-flight schema-1 SFTP upload/fetch records resume when the forced
   gate removes SFTP; preserve their original request IDs and receipts. Clarify
   whether restart artifacts count toward the 128 input-file limit, and whether
   archive-only fetch keeps its current fixed limits. The restart size exception
   itself is decided and must not inherit the old 2/4 GiB caps.
3. **D08 flow sequencing (P3b):** the existing registration path requires published
   inputs. D08 explicitly bars release until every restart artifact is verified,
   but does not say whether Hold may be registered while restart transfer is
   pending. The proposed simpler all-transfers-before-Hold flow is a draft;
   choose the intended sequencing before designing its states/receipts. Both
   release and launch must enforce the final artifact barrier in either case.
4. **D10/D11 event integration (P1):** current SSH failures do not fully distinguish
   authentication from transport disconnection. Specify trustworthy classification
   and retry policy, including host-key/readiness refusals, without relying on
   arbitrary stderr substrings as sufficient authority. Pure `None` initial
   state, handshake-only recovery and fresh `<= 15 s` are executable Phase 0
   interpretations; review their UI wording without weakening mutation guards.
5. **D11 telemetry-expiry conflict (P1):** in the §3.5 measurement, six of nine
   warm CPU observations reported `warming-up`: the existing `MAX_AGE = 5.0` s
   baseline cutoff conflicts with 5 s polling plus scheduling/verification
   jitter. Proposed Phase 1 resolution: sample telemetry on the node at its own
   cadence, as the thermal provider already does, and have `status` return the
   latest sample with its age. Status polling then neither drives sampling nor
   determines whether the CPU baseline expires. Retain explicit stale and
   unavailable evidence when the node sampler stops or samples age out; choose
   and test the node sampling cadence and aging policy in P1.
6. **D12 trust-preserving factoring (P1):** the baseline performs two complete
   runtime scans. Validate that one scan plus complete response-boundary evidence
   preserves replacement/security/loaded-code refusal and legacy behavior on
   supported platforms. Set performance goals from the recorded measurement;
   do not replace full evidence with a TTL or root-mtime cache. Any hardware
   evidence caching requires its own explicit identity/boot rationale.
7. **D13–D15 runtime migration (P3a):** the existing worker turns any
   `cancel_requested` into hard termination after 2 s. New graceful/soft requests
   therefore cannot be wired by setting only that flag. Define mode record
   compatibility and atomic history/STOPCAR ownership, pending-job cancellation,
   terminal repeats, suspended jobs, lost ownership and failed cleanup. Preserve
   legacy hard requests and terminal `Cancelled`; do not reinterpret old data.
   Only VASP and ORCA mode availability is specified; no new graceful/soft
   meaning is assigned to Multiwfn or generic Command. The pure availability
   function returns no newly specified modes for those engines; it does not
   remove their existing runtime cancellation capability.
8. **D17–D19 preparation and runner conflict (P3a):** current node VASP argv sends
   `--ncore`/`--kpar`, fixes `std` and `restart none`, and the current port route
   rewrites parallelization. In this repository,
   [`preparation.py`](../../src/cmw/periodic/vasp/preparation.py) rejects NPAR and
   permits only NCORE/KPAR in its runtime overlay, although the generic parser
   recognizes NPAR. A correct pure NPAR plan does not make that preparation route
   supported. Coordinate a compatible author-input preparation/port interface
   and its provenance/finalization rules without silently editing inputs. Specify
   how a runner profile expresses NCORE when NPAR overrides it: no invented
   scientific NCORE is selected here. The separate port PR owns verify-only
   launch behavior; P0 changes neither repository's runtime path.
9. **D20/D22 capability data and rollout:** current host evidence supplies platform
    UUID, not the new physical-core/tier/policy object, and node submission fixes
    `std`. Define hardware discovery, incomplete evidence reporting, build
    provenance fields and explicit experimental opt-in in negotiated versions.
    Tier names stay native/uninterpreted; policy stays nullable. The exact phase
    for endpoint advertisement must precede consumers that require these facts.
10. **D23 remote-origin boundary (P3b):** generic Command is currently accepted
    remotely. The endpoint must receive trusted origin independently of client
    JSON and enforce the typed-profile rule across all remote registration
    routes, while retaining access to old records and existing typed jobs.
    Decide capability rollout/refusal for older endpoints before enabling the
    new policy; the gate alone cannot provide complete enforcement.
11. **D26 journal (P4):** decide durable append/receipt atomicity, sequence recovery,
    migration/linkage to queue history, retention and export. The decision calls
    for permanent node-local history, not cross-node total time ordering.
