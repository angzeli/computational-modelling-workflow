# Multi-host `cmw jobs`

**Status:** Draft — Phase 0 design contract
**Date:** 2026-11-04
**Scope:** Unified job monitoring and control across `mbp_m3max` and `mm_m5pro`, with symmetric VASP submission

This document defines the architecture, wire protocol, and safety semantics for running `cmw jobs` across more than one machine. It is normative for Phases 1–6: implementation that contradicts it should change this document first, through review, rather than diverge silently.

The key words MUST, MUST NOT, SHOULD, and MAY are used in their usual normative sense.

---

## 1. Goals and non-goals

### Goals

- One portal that shows and controls jobs on every configured node, filterable by node.
- Full symmetry: any node can monitor, submit to, and cancel jobs on any other node, including itself.
- VASP inputs prepared on any node can be transferred to and started on any other node.
- Explicit, inspectable failure semantics for network loss, version skew, and authentication failure.
- Cancellation semantics precise enough that a human or a coding agent asking for a "hard", "soft", or "graceful" cancel gets exactly one well-defined behaviour.

### Non-goals (for this design)

- Cluster schedulers, HPC submission, or any node outside the user's own tailnet.
- Transferring large output files (WAVECAR, CHGCAR, `.gbw`) — deferred to Phase 5.
- ORCA submission across nodes — the bundle mechanism is designed to extend to ORCA later, but only VASP is in scope.
- Automatic retries of scientific calculations.
- Any change to how a single node's controller admits, runs, or finalizes jobs.

---

## 2. Terminology

| Term | Meaning |
|---|---|
| **Node** | A machine running a `cmw jobs` controller, identified by a `node_id`. |
| **Owning node** | The node whose controller owns a given job, queue, or file. |
| **Portal** | The aggregator UI/CLI that collects snapshots from nodes and issues requests to them. A portal runs on any node. |
| **Transport** | The mechanism a portal uses to reach a node: `LocalTransport` for the node the portal runs on, `SshTransport` for others, `FakeTransport` in tests. |
| **Bundle** | The exact-byte VASP input set plus a manifest, transferred from the authoring node to the executing node. |
| **`request_id`** | A client-generated unique identifier that makes submit idempotent across retries. |

---

## 3. Architecture

```
            ┌────────────────────────── portal (on any node) ──────────────────────────┐
            │  aggregator: snapshots, node states, host filter, submit/cancel requests │
            └──────────────┬─────────────────────────────────────────┬─────────────────┘
                           │ LocalTransport                          │ SshTransport
                           ▼                                         ▼
              ┌──────────────────────────┐            ┌──────────────────────────┐
              │ node: mbp_m3max          │            │ node: mm_m5pro           │
              │ cmw jobs node <command>  │            │ cmw jobs node <command>  │
              │ controller · queue · locks│           │ controller · queue · locks│
              └──────────────────────────┘            └──────────────────────────┘
```

The portal is a pure aggregator. Every node exposes the same `cmw jobs node` command surface, and the local node is reached through exactly the same protocol as a remote one, with only the transport differing.

### 3.1 Invariants

1. **Owner-only mutation.** A portal MUST NOT read or write a node's queue, state, lock, staging, or job files directly. Every observation and mutation goes through that node's own `cmw jobs node` commands, executed by that node.
2. **No local special case.** Submitting to, cancelling on, or monitoring the node the portal runs on MUST use the same protocol path as for a remote node.
3. **Node-owned layout.** The executing node chooses where a job lives, via its own ExecutionLayout v2. A requester MUST NOT choose or supply a remote filesystem path.
4. **Node-owned admission.** A submitted job enters the executing node's queue. Whether and when it starts is decided by that node's admission policy (Sequential or Bounded Sharing); a portal cannot bypass it.
5. **No inference across a broken link.** When a node is not `REACHABLE`, the portal MUST present that node's jobs as last-known state and MUST NOT infer completion, failure, or idleness.
6. **Execution is not validity.** A cancelled job is never a successfully completed calculation, regardless of how cleanly it stopped.
7. **Licensed data stays bounded.** POTCAR content MUST NOT appear in logs, job log entries, error messages, or portal output. Only hashes and dataset identities are recorded.

---

## 4. Node identity

| `node_id` | Machine |
|---|---|
| `mbp_m3max` | MacBook Pro, M3 Max |
| `mm_m5pro` | Mac mini, M5 Pro |

- Format: `^[a-z0-9_]{1,32}$`.
- A `node_id` is immutable once any job log entry or record references it. Replacing hardware does not rename a node; a new machine gets a new `node_id`.
- `node_id` is set in node configuration, not derived from hostname or Tailscale machine name.

---

## 5. Transport

### 5.1 SSH

Remote nodes are reached through macOS's built-in OpenSSH server (System Settings → Remote Login), restricted to the account that runs `cmw`.

Each portal uses a dedicated key. The executing node's `authorized_keys` entry pins that key to a single gate program:

```
restrict,command="/Users/<user>/cmw/bin/cmw-node-gate" ssh-ed25519 AAAA... cmw-portal@mbp_m3max
```

`restrict` disables port, agent, and X11 forwarding and PTY allocation.

### 5.2 Gate program

The gate reads `SSH_ORIGINAL_COMMAND` and MUST:

- parse it into an argument vector without invoking a shell;
- accept only `cmw jobs node <command>` with a whitelisted `<command>` and whitelisted argument patterns;
- execute the configured **absolute** path to `cmw` with that argument vector;
- reject everything else with a protocol error and a non-zero exit code.

The absolute path matters because non-interactive SSH sessions do not load the user's interactive shell configuration, so `cmw` is usually not on `PATH`.

### 5.3 Client configuration

```
Host mm_m5pro
    HostName <tailnet-name-or-address>
    User <user>
    IdentityFile ~/.ssh/cmw_portal_ed25519
    IdentitiesOnly yes
    BatchMode yes
    ControlMaster auto
    ControlPath ~/.ssh/cm-%C
    ControlPersist 10m
    ConnectTimeout 4
    ServerAliveInterval 5
    ServerAliveCountMax 2
```

`BatchMode yes` guarantees the transport never blocks on an interactive prompt. `ControlMaster` reuses one authenticated connection, so each poll opens a channel rather than a new SSH handshake.

### 5.4 macOS privacy restrictions

Remote Login sessions cannot access TCC-protected folders (`~/Documents`, `~/Desktop`, `~/Downloads`) by default. All job, staging, and Scratch directories used by `cmw jobs` MUST live outside them, for example under `~/cmw/`.

---

## 6. Wire protocol

### 6.1 Command surface

| Command | Purpose | Mutates |
|---|---|---|
| `cmw jobs node hello` | Handshake: identity, versions, capabilities, clock | No |
| `cmw jobs node snapshot` | Queue, running jobs, resources, telemetry | No |
| `cmw jobs node receive` | Accept a bundle on stdin into staging | Staging only |
| `cmw jobs node submit` | Enqueue a staged bundle as a job | Yes |
| `cmw jobs node cancel` | Request or escalate cancellation | Yes |
| `cmw jobs node tail` | Bounded read of a job file *(Phase 5)* | No |

### 6.2 Response envelope

Every command writes exactly one JSON object to stdout:

```json
{
  "protocol_version": "1.0",
  "node_id": "mm_m5pro",
  "command": "snapshot",
  "request_id": "optional, echoed when supplied",
  "ok": true,
  "result": {},
  "error": null
}
```

On failure, `ok` is `false`, `result` is `null`, and `error` is `{"code": "<stable_code>", "message": "<human text>", "details": {}}`. Portals branch on `code`, never on `message`.

### 6.3 Exit codes (proposed)

| Exit | `error.code` | Meaning |
|---|---|---|
| 0 | — | Success |
| 2 | `invalid_request` | Malformed arguments or payload |
| 10 | `protocol_incompatible` | Major version mismatch |
| 11 | `capability_unsupported` | Command or mode not offered by this node |
| 20 | `not_found` | Unknown job or staged bundle |
| 21 | `conflict` | e.g. cancel downgrade, `request_id` reused with different content |
| 22 | `validation_failed` | Bundle or input validation failed |
| 40 | `internal_error` | Unexpected node-side failure |

Transport-level failures (SSH cannot connect, authentication rejected, timeout) are detected by the transport, not reported through the envelope.

### 6.4 Handshake and versioning

`protocol_version` is `MAJOR.MINOR`.

- Different MAJOR: the portal MUST mark the node `INCOMPATIBLE` and disable every operation on it.
- Same MAJOR, different MINOR: the portal enables only features present in both sides' `capabilities`.

`hello` result:

```json
{
  "node_id": "mm_m5pro",
  "protocol_version": "1.0",
  "cmw_version": "0.1.0",
  "cmw_commit": "<git sha>",
  "node_time_utc": "2026-11-04T12:00:00.000Z",
  "hardware": {
    "physical_cores": 18,
    "core_tiers": [
      { "level": 0, "name": "<sysctl hw.perflevel0.name>", "physical_cores": 12 },
      { "level": 1, "name": "<sysctl hw.perflevel1.name>", "physical_cores": 6 }
    ],
    "recommended_max_parallelism": null
  },
  "capabilities": {
    "submit": true,
    "receive": { "max_bundle_bytes": 67108864 },
    "cancel_modes": { "vasp": ["graceful", "soft", "hard"], "orca": ["hard"] },
    "engines": {
      "vasp": {
        "builds": [
          {
            "build_id": "<port commit or label>",
            "version": "6.6.1",
            "executables": ["vasp_std", "vasp_gam", "vasp_ncl"],
            "experimental": false,
            "default": true
          }
        ]
      }
    }
  }
}
```

`core_tiers` reports native facts read from sysctl. Each `name` is the verbatim
`hw.perflevelN.name` value: implementations MUST NOT hard-code, rename or interpret
tier names. The values above are illustrative, not observations of either node.
`recommended_max_parallelism` is node policy read from node configuration;
`null` means not configured. It MUST NOT be inferred from tier names or counts.
Phase 0 models these declarations without reading sysctl or configuration.

The portal performs a handshake on first contact and again after every transition out of `UNREACHABLE`, `AUTH_FAILED`, or `INCOMPATIBLE`.

---

## 7. Node states

| State | Entered when | Portal behaviour |
|---|---|---|
| `REACHABLE` | Handshake compatible and latest request succeeded | Live display; operations enabled |
| `UNREACHABLE` | 3 consecutive transport failures (connect error or timeout) | Last-known snapshot, greyed, with its age; operations disabled |
| `INCOMPATIBLE` | Handshake reports a different MAJOR version | Version comparison shown; operations disabled |
| `AUTH_FAILED` | SSH authentication rejected | Key/config hint shown; automatic polling of that node suspended until a manual retry |

Rules:

- `INCOMPATIBLE` and `AUTH_FAILED` are entered on the first occurrence; retrying cannot fix them.
- After one or two failures the node stays `REACHABLE`, and the portal shows the snapshot age.
- Any successful request resets the failure counter. Leaving `UNREACHABLE` requires a fresh handshake.
- All staleness and reachability judgements use the **portal's** receive time, never the node's clock.

---

## 8. Polling

| Parameter | Value |
|---|---|
| Foreground interval | 5 s |
| Background interval (portal hidden or idle) | 30 s |
| SSH connect timeout | 4 s |
| Snapshot command timeout | 4 s |
| Overlap | Never: a node is not polled again while its previous poll is in flight |

Expected time from link loss to `UNREACHABLE` is roughly 15–25 s.

`snapshot` SHOULD read a status file that the controller already maintains, rather than recomputing node state on each call, so a poll costs little more than interpreter start-up on the node. Polling overhead on production nodes SHOULD be measured once Phase 1 exists.

---

## 9. Clock skew

The portal estimates skew from `hello.node_time_utc` against the midpoint of its own send and receive times.

| Skew | Portal display |
|---|---|
| < 5 s | Nothing |
| ≥ 5 s | Amber warning |
| ≥ 60 s | Red warning |

No system logic depends on clocks agreeing across nodes. Reachability uses portal time (§7), and each node orders its own job log entries with a node-local monotonic sequence number; timestamps are for display only.

---

## 10. VASP submission

### 10.1 Flow

1. **Prepare on the authoring node.** `cmw vasp prepare` produces the exact-byte four-file input set (INCAR, KPOINTS, POSCAR, POTCAR). The preparation record stays in the authoring node's Scratch.
2. **Plan.** `cmw jobs submit --node <node_id> --plan` performs a handshake and runs every check in §10.4 that can be evaluated locally against the target's capabilities. It writes nothing anywhere. The portal's confirmation dialog shows the same plan.
3. **Transfer.** The bundle is streamed to `cmw jobs node receive` on the target (§10.3).
4. **Validate on target.** The target re-verifies hashes and runs its own input checks (§10.4).
5. **Enqueue.** `cmw jobs node submit` with the same `request_id` enqueues the staged bundle and returns a `job_id`.
6. **Record.** The target writes a receipt record in its own Scratch (§10.6).

The same flow applies when the target is the authoring node itself.

### 10.2 Bundle

```
manifest.json
inputs/INCAR
inputs/KPOINTS
inputs/POSCAR
inputs/POTCAR
```

`manifest.json`:

```json
{
  "schema_version": "1.0",
  "request_id": "<uuid>",
  "source_node_id": "mbp_m3max",
  "created_utc": "2026-11-04T12:00:00.000Z",
  "engine": "vasp",
  "executable": "vasp_std",
  "build": { "build_id": null, "allow_experimental": false },
  "execution": { "mpi_ranks": 16, "omp_threads": 1 },
  "files": [
    { "path": "inputs/INCAR", "size": 512, "sha256": "<hex>" }
  ],
  "provenance": {
    "preparation_record_sha256": "<hex>",
    "potcar_identity": [
      { "element": "Zn", "variant": "Zn", "titel": "<TITEL line>", "sha256": "<hex>" }
    ]
  }
}
```

- POTCAR is prepared on the authoring node and transferred; the executing node does not need a POTCAR library.
- `build.build_id = null` means "the target's default production build".
- Parallelisation is decided by the author. INCAR is transferred and executed byte-for-byte unchanged, and `mpi_ranks` and `omp_threads` travel in the manifest because they must agree with INCAR's `KPAR` and `NCORE`.
- The target's node profile supplies only environment: executable paths, MPI launcher path, and environment variables.

### 10.3 Transfer: `node receive`

The bundle is a tar stream on stdin.

- `manifest.json` MUST be the first member. The node parses and validates it before reading anything else.
- Every later member MUST be a regular file whose path is listed in the manifest. Absolute paths, `..` components, symlinks, hard links, device files, and duplicate paths are rejected.
- Each file's size and SHA-256 MUST match the manifest. Every manifest file MUST be present.
- Total size MUST NOT exceed `max_bundle_bytes` (default 64 MiB).
- Members are validated by the node's own checks, not by relying on a particular Python version's `tarfile` extraction filter.
- The node writes to `staging/<request_id>.partial/`, fsyncs, then atomically renames to `staging/<request_id>/`.

Idempotency:

- Same `request_id`, identical manifest: return the existing staged result without rewriting.
- Same `request_id`, different manifest: `conflict`.

### 10.4 Validation

| Check | Where | Severity |
|---|---|---|
| Bundle size within target's `max_bundle_bytes` | plan, receive | Error |
| File hashes match manifest | receive | Error |
| POSCAR species order matches POTCAR | plan, receive | Error |
| Requested executable exists in the selected build | plan, receive | Error |
| Selected build is experimental but `allow_experimental` is false | plan, receive | Error |
| `mpi_ranks × omp_threads` exceeds target `physical_cores` | plan, receive | Error |
| `mpi_ranks × omp_threads` exceeds target `recommended_max_parallelism` (skipped when `null`) | plan, receive | Warning |
| `mpi_ranks` not divisible by INCAR `KPAR` | plan, receive | Error |
| `NCORE` incompatible with `mpi_ranks / KPAR` | plan, receive | Warning |

Validation reads INCAR; it never modifies it.

### 10.5 Submit

- `node submit` takes the `request_id` of a staged bundle and returns a `job_id`.
- Repeating `submit` with the same `request_id` returns the original `job_id` and never creates a second job.
- If the link drops after `submit` is sent, the requester MAY safely resend it with the same `request_id`.
- The portal's confirmation dialog MUST display the target `node_id` prominently.

### 10.6 Receipt record

Written to the executing node's Scratch: `request_id`, `job_id`, `source_node_id`, manifest SHA-256, per-file hashes, `preparation_record_sha256`, POTCAR identities (no content), the `build_id` actually used, executable, `mpi_ranks`, `omp_threads`, and receive time. Together with the authoring node's preparation record, this gives exact-byte lineage across nodes.

---

## 11. Cancellation

### 11.1 Modes

| Mode | VASP mechanism | Stops | Output state |
|---|---|---|---|
| `graceful` | STOPCAR `LSTOP = .TRUE.` | After the current ionic step | Final ionic step's SCF converged; CONTCAR/WAVECAR from normal shutdown |
| `soft` | STOPCAR `LABORT = .TRUE.` | After the current electronic step | Normal shutdown; final ionic step's SCF **not** converged |
| `hard` | SIGTERM to the process group, SIGKILL after a 10 s grace period | Immediately | Output files may be incomplete or truncated |

These three names are the only cancellation vocabulary. Synonyms such as `--force` or `--now` MUST NOT be introduced.

### 11.2 Escalation

```
graceful  →  soft  →  hard
```

| Current | Requested | Result |
|---|---|---|
| none | any | Start cancellation in that mode |
| `graceful` | `soft` or `hard` | Escalate |
| `soft` | `hard` | Escalate |
| same mode | same mode | No-op, success |
| stronger | weaker | `conflict`, reporting the current mode |

There is no automatic timeout or escalation; `graceful` may legitimately wait hours on a hybrid-functional ionic step. Escalation is always an explicit request.

Because repeating the same mode is a no-op, a requester whose link dropped mid-cancel MAY safely resend the same request. Until it does, the portal shows the cancel state as unknown.

### 11.3 Mechanics

- STOPCAR is written atomically (temporary file, then rename) by the owning node into the running job's directory.
- Escalating `graceful` → `soft` atomically replaces STOPCAR with `LABORT = .TRUE.`. Escalation takes effect at VASP's next STOPCAR check.
- After the VASP process has exited, by any mode, the owning node removes STOPCAR and records its removal, so a later resume in the same directory is not stopped immediately.

### 11.4 State and results

Cancellation state is reported in `snapshot` and in every `cancel` response:

```json
{
  "cancel": {
    "mode": "soft",
    "state": "in_progress",
    "history": [
      { "mode": "graceful", "requested_utc": "...", "by_node_id": "mm_m5pro" },
      { "mode": "soft",     "requested_utc": "...", "by_node_id": "mbp_m3max" }
    ]
  }
}
```

`state` is one of `requested`, `in_progress`, `completed`, `failed`.

A job that ends through any cancel mode reaches the terminal state `CANCELLED` with its effective mode recorded. `cmw vasp finalize-result` MUST refuse to treat a `CANCELLED` job as a completed result.

### 11.5 Engine capabilities

Cancel modes are advertised per engine in `hello.capabilities.cancel_modes`. VASP offers all three; ORCA offers only `hard`. The portal offers only the modes the job's engine supports; a request for an unsupported mode returns `capability_unsupported`.

### 11.6 CLI and agent contract

```
cmw jobs cancel <job_id> --node <node_id> --mode graceful|soft|hard
```

When the cancel command is implemented, `AGENTS.md` gains a section that reproduces §11.1 and §11.2 verbatim, so coding agents use the same vocabulary and rules as humans. Agents confirm the outcome by reading the JSON response, not by parsing text.

---

## 12. Security summary

- SSH only, inside the user's tailnet, with one dedicated key per portal.
- `restrict` plus a forced gate command: a leaked portal key cannot obtain a shell.
- The gate never invokes a shell and whitelists commands and argument patterns.
- `BatchMode yes`: the transport never prompts.
- Nodes write only inside their own staging, Scratch, and job layout; requesters never supply paths.
- Bundles are size-bounded and validated member by member.
- POTCAR content never appears in logs, errors, or portal output.

---

## 13. Testing strategy

All tests are synthetic and offline, consistent with the rest of the repository: no SSH, VASP, or network access is required. `FakeTransport` supports scripted scenarios:

- latency, timeouts, and disconnects after N requests;
- authentication failure;
- MAJOR and MINOR version mismatch, including capability negotiation;
- clock skew around the 5 s and 60 s thresholds;
- truncated tar streams;
- hostile bundles: path traversal, absolute paths, symlinks, hard links, extra files, missing files, oversize bundles, hash mismatches;
- reused `request_id` with identical and with different manifests;
- cancel escalation, no-op repeats, and rejected downgrades;
- the full state-machine transition table in §7.

---

## 14. Phase plan

| Phase | Content |
|---|---|
| 0 | This document; protocol, manifest, and capability schemas; typed models; pure contract logic; transport interface with `FakeTransport` |
| 1 | Structural refactor: the local node becomes one node behind `LocalTransport` |
| 2 | Permanent job log (with `node_id` and per-node sequence numbers) and per-engine evidence sources, including `stdout.log` |
| 3 | `SshTransport`, gate program, unified portal with node filtering; bidirectional monitoring, submit, and cancel |
| 4 | Thermal telemetry on every node |
| 5 | Bounded remote file viewing (`node tail`), starting with a time-boxed spike |
| 6 | UI polish and remaining clean-up |

---

## 15. Open questions

1. **Reconciliation with existing code.** This draft was written without reading the current `cmw jobs` implementation or PR #3. Where the existing controller, resource-request model, Scratch layout, or current remote portal differs from this document, record the difference here before changing either side.
2. **`--mode` default.** Proposed: `--mode` is required with no default, so no cancellation happens without an explicit choice.
3. **Hard-cancel grace period.** Proposed: 10 s between SIGTERM and SIGKILL.
4. **Laptop power state.** Should `mbp_m3max` refuse remote submissions while on battery, and should the controller hold a `caffeinate` assertion while jobs run?
5. **Resource declarations in the manifest.** How the existing request/commitment model maps onto a remotely submitted job.
6. **Key topology.** One key per directed pair (`mbp_m3max → mm_m5pro` and the reverse) is assumed.


### Phase 0 reconciliation against the existing implementation

The implementation inspected for this phase is the locally available
`origin/main` commit `1563c2f` (PR #3 merge). No network refresh was performed.
PR #3 is the lean-CI change and includes one portal request-bookkeeping line;
it did not introduce the existing remote-node implementation. The questions
below preserve the original decisions, with the explicitly requested hardware
addendum incorporated in §§6.4 and 10.4. They identify integration work, not
permission to alter existing behavior in Phase 0. The new package has no imports from existing
runtime entry points and is not wired into a CLI command.

**Adapted conventions.** The existing Jobs package is `src/cmw/jobs`, so the
new contract-only package is `src/cmw/jobs/multihost`. Standard-library typed
models follow the repository's existing dataclass/strict-JSON conventions.
Existing queue, node, preparation and result records retain their formats.

7. **Existing node API and protocol migration.** `src/cmw/nodes` already
   implements a local backend, a persistent SSH endpoint, a unified portal,
   submission, controls and selected-file transfer. Its command is
   `cmw nodes endpoint`, with integer protocol `1`, bounded JSON-lines frames,
   `capabilities`/`status` operations, uppercase error codes and observation
   envelopes containing queue/session/sequence/clock/runtime/host identities.
   How will this coexist with or migrate to the §6 command and envelope
   contract? Phase 0 adds separate inactive contracts; it does not translate,
   replace or weaken the existing protocol.
8. **Node and queue identity.** Existing node, queue and storage identities
   are UUID hex values, while display/connection aliases are separate. Selected
   jobs bind node, queue, internal job ID and immutable attempt ID. §4 uses
   human-readable node IDs and the new envelope has no queue identity. How
   will aliases, durable identities and multiple enrolled queues map without
   silently retargeting existing records? The Phase 0 node-ID syntax follows
   §4; existing identities are unchanged.
9. **SSH security and transport lifetime.** The existing transport pins an
   enrolled SSH host key and runtime/hardware identity, disables ControlMaster,
   and keeps one bounded endpoint stream with one outstanding request. §§5
   and 12 require a forced-command gate and multiplexed command channels.
   How will the existing identity checks and observation-session semantics
   be preserved during that change? The new real transports remain skeletons;
   no SSH configuration, keys, endpoint or security rules change here.
10. **Portal data and polling.** The current node backend reads the local
    Store and samples activity/telemetry; the controller does not maintain
    the status file assumed by §8. The current portal polls at two seconds,
    treats observations older than ten seconds as stale, and marks transport
    failure immediately with exponential retry backoff. §§7–8 instead specify
    three consecutive failures and five/thirty-second intervals. The new
    pure state machine uses §7; choosing/publishing a cached snapshot and
    integrating polling remain later-phase decisions.
11. **Initial and recovering node state.** §7 does not name a state before
    the first successful handshake. Phase 0 represents it as unconfirmed
    (`None`) with operations disabled, without adding a fifth wire state.
    A successful ordinary request resets consecutive transport failures but
    cannot restore operations from an unconfirmed or blocked state without
    a fresh compatible handshake. Authentication recovery requires a manual
    retry; no automatic poll is authorized by the pure model. Future portal
    presentation and retry orchestration must preserve these boundaries.
12. **Plan side effects.** Current remote `submit --preview` creates a private
    frontend spool and durable request record before the target preview.
    §10.1 `--plan` writes nothing anywhere. A later command adapter must provide
    the specified read-only plan rather than relabeling the existing preview.
13. **Submission intent and resource mapping.** Current remote submission
    requires Hold-only intent, reviewed dependencies, explicit `cpus` and
    `memory_gib`, optional `mpi_ranks`/`threads_per_rank`, and enrolled storage
    and runner profiles. Release and dispatch are separately authorized.
    The §10.2 manifest supplies `mpi_ranks`/`omp_threads` but no memory,
    scheduling role, sharing consent, target queue/profile, or Hold/Queue
    intent. How will these map to current admission and commitment rules?
    Phase 0 checks only the declared contract and supplies none of those
    missing operational or scientific choices.
14. **Node-owned layout and Scratch.** Existing remote submission places
    bundles and runs under the enrolled storage namespace
    `cmw-<node UUID>/prepared/<request_id>` and `runs/<request_id>/native`;
    it does not use ExecutionLayout v2 for that route. Canonical VASP
    preparation publishes four inputs and a separate caller-selected Scratch
    `preparation.json`, not an adjacent sidecar or a fixed global Scratch
    directory. How will node-owned v2 layouts, staging and receipt records
    compose with these existing publication/identity contracts? No paths or
    publication behavior change in Phase 0.
15. **Transfer format, limits and POTCAR authorization.** Current transfer
    uses selected-file SFTP with `inputs/` and `support/`, 128 files, a
    64 MiB per-file limit, a 256 MiB aggregate limit, private modes and an
    explicit `potcar_authorized` declaration. §§10 and 12 instead specify a
    tar stream, exactly the four VASP inputs and a 64 MiB aggregate default.
    How will the existing authorization and transfer receipts migrate?
    Phase 0 validates the new format without changing the old route. The
    §10.2 one-entry `files` example is illustrative: the normative bundle
    requires all four inputs. `manifest.json` is the first-member exception
    to the later-member allowlist and is not self-listed or self-hashed.
16. **Bundle byte count.** §10 does not say whether `max_bundle_bytes` counts
    tar headers, padding and end markers. Phase 0 requires an explicit
    manifest payload size and counts it plus the listed input payload sizes;
    it does not infer the original size from re-serialized JSON. Raw tar
    framing/stream limits and truncated-stream detection remain a receive
    implementation decision. Header validation cannot certify that bytes
    were received or that a stream ended correctly.
17. **Manifest equality and idempotency.** §§10.3–10.5 do not distinguish
    byte-for-byte JSON equality from equality of parsed JSON content.
    Existing request receipts use canonical structured-content digests.
    Phase 0 compares the complete parsed manifest as canonical JSON, including
    provenance and timestamps, preserving array order while ignoring object
    key order and whitespace. Changed content conflicts. Future transfer and
    receipt code must settle this interpretation before persisting an
    interoperable manifest digest; Phase 0 writes no receipts.
18. **VASP runner/build selection.** Current submission selects pinned
    runner profiles and requires explicit `ncore`, `kpar`, `mpi_mode`, timeout
    and stop-before settings, along with qualified rank limits and restart
    restrictions. The design selects a build/executable and requires unchanged
    INCAR bytes. How will the runner preserve those bytes while honoring the
    author-selected parallelism and existing admission checks? The pure plan
    checks use the target's advertised build inventory; they neither execute
    nor probe a runner or infer a target path.
19. **Incomplete plan evidence.** §10.4 does not specify absent INCAR
    `KPAR`/`NCORE` handling or how parsed POSCAR species reach the plan.
    Phase 0 leaves the corresponding checks unassessed when that parsed
    evidence is absent; it does not invent scientific values or assert a
    successful input check. With supplied positive values, KPAR must divide
    the rank count and NCORE incompatibility with `mpi_ranks / KPAR` is a
    warning. Ordered species evidence must come from the existing parsers;
    hashes or a claimed POTCAR identity alone do not prove a POSCAR match.
20. **POTCAR and preparation identities.** Existing POTCAR selection keeps
    parsed element/variant/family/date, terminated dataset-span `sha256`,
    `source_sha256`, `assembled_chunk_sha256` and whole-file `potcar_sha256`;
    these are different identities. It does not retain the exact raw TITEL
    line required by §10.2. Future bundle creation must establish which
    dataset identity populates each entry and how to obtain metadata without
    exposing licensed payloads. `preparation_record_sha256` means the exact
    authoring record bytes, not the existing `preparation_content_id`, which
    hashes calculation intent, prepared identities and expected runtime.
    Phase 0 accepts explicit synthetic identity metadata and reads no real
    POTCAR or preparation record.
21. **Cancellation and terminal spelling.** Existing cancellation records
    `cancel_requested`, enters `Cancelling`, and automatically advances TERM
    to KILL after two seconds using verified process ownership. It has no
    graceful/soft mode history. §11 requires explicit escalation, STOPCAR
    modes and a ten-second hard grace period; existing public/persisted
    terminal state is `Cancelled`, whereas §11.4 spells it `CANCELLED`.
    Future integration must settle the public/protocol mapping and update
    lifecycle behavior through review. Phase 0 adds the new pure decisions
    without touching runtime signals, STOPCAR or Store states. Existing VASP
    finalization already requires `Done` with completion evidence and does
    not accept a cancelled attempt as a successful result.
22. **Preserving current capability boundaries.** Existing nodes already
    expose ORCA/Multiwfn submission, bounded logs/file fetch, thermal readings,
    catalogue/result-review/study operations and controlled suspension.
    Several are outside or assigned to future phases in this design. How
    will their existing contracts remain available during the refactor?
    Phase 0 does not remove, reclassify or wire any existing capability.
23. **Capability negotiation details.** §6.4 specifies feature intersection
    but does not define intersections of nested limits or build inventories.
    Target build IDs are execution choices, not a requirement that the portal
    host install an identical build. Phase 0 keeps target selection separate
    from shared feature negotiation; any future unsupported capability or
    unresolved build choice must remain explicit rather than silently
    authorizing an operation.
24. **Strict parsing and extension boundaries.** Phase 0 models accept MAJOR 1
    and preserve compatible MINOR versions. Structural objects reject unknown
    fields; capability names may be omitted (unsupported), and unknown names
    round-trip as inert data rather than enabling operations. Future extensions
    to structural objects need a reviewed schema update. The pure version
    comparison compares the caller's declared local and remote MAJOR values;
    it is not evidence that this installed parser supports another MAJOR.
    JSON Schemas describe wire structure; models also enforce cross-field
    identities such as matching hello/envelope identity and unique build IDs.
    Cancellation history contains only effective requests, strictly increasing
    in strength and ending at the reported mode. Repeated requests add no event;
    history is not sorted by timestamps from potentially skewed nodes.
25. **Hardware facts and node policy.** The superseding Phase 0 addendum adds
    verbatim native `core_tiers` and nullable configured
    `recommended_max_parallelism` to §6.4. Tier names carry no interpreted
    scheduling meaning. An empty tier list supplies no tier facts; Phase 0
    neither discovers tiers nor derives a policy value. The hard capacity
    check uses `physical_cores`; only a configured recommendation adds the
    separate warning in §10.4. These contracts do not introduce sysctl calls
    or modify existing telemetry or admission.
