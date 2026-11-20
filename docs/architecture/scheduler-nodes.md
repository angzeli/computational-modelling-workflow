# Scheduler-backed nodes: PBS Pro design

Status: **Phase 0 design. No code or deployment.** The read-only fact probe
ran on 2026-11-20 against one login node and is summarized in
[§6](#6-probe-results); F09, F11 (idle lifetime) and F14 are still open. This document is the Phase 0 deliverable of
[issue #81](https://github.com/angzeli/computational-modelling-workflow/issues/81).
It extends the [multi-host nodes delta specification](multi-host-nodes.md)
(decisions D01–D34) with decisions S01–S16 for one new node kind, `pbs`. It
introduces no second jobs protocol, manifest, provenance format, portal or
scheduler of CMW's own. The [nodes contract](../cmw-dual-node.md) and
[Jobs contract](../cmw-jobs.md) remain the description of supported behaviour.

The target is a PBS Pro cluster reached through shared login nodes. Its facts
in §1 come from the site's user documentation, and the
[fact probe](#5-fact-probe-checklist) confirms them in a separately authorized
window. Phase 1 must not start from an unconfirmed fact that a decision depends
on. Details that identify the site, such as host, server and queue names,
module versions and filesystem paths, are kept in the owner's local notes and
are not recorded in this repository.

## 1. Baseline on `main`

The current node model assumes each node runs its own Jobs runtime on a
directly attached disk. The target cluster breaks that assumption in these
places:

| Area | Implementation on `main` | Target cluster |
| --- | --- | --- |
| SSH transport | [`transport.py`](../../src/cmw/nodes/transport.py) builds one fixed argv with `BatchMode=yes`, `StrictHostKeyChecking=yes`, `ControlMaster=no` and `ControlPath=none`. Connections use key authentication with a pinned host key. | Key authentication is disabled on the login nodes. Login needs the site password, from the site network or its VPN. |
| Host identity | [`identity.host_evidence`](../../src/cmw/nodes/identity.py) uses the platform UUID on macOS and `/etc/machine-id` on Linux, never a hostname. | The login name resolves to several login nodes. The payload runs on a compute node, which is a different host again. |
| Dispatch | A detached controller on the node dispatches from the queue. The [Jobs limits](../cmw-jobs.md) exclude detached scheduler workers and remote launches. | Login nodes are not for long-running work. PBS decides when and where a job starts. |
| Process ownership | D34 binds ownership to host, boot session, PID and kernel start token. [`runtime.worker`](../../src/cmw/jobs/runtime.py) is the guardian: [`payload.sh`](../../src/cmw/jobs/payload.sh) waits for its `GO` and `RELEASE`. | An endpoint on a login node cannot observe a compute-node process. |
| Storage | [`storage.platform_volume`](../../src/cmw/nodes/storage.py) fails closed on network filesystems. On Linux, `local` requires a `findmnt` source under `/dev/`. | The home filesystem, and a scratch filesystem that deletes files after a fixed period, are network filesystems. |
| Databases | The queue [`Store`](../../src/cmw/jobs/store.py) requires the rollback (`delete`) journal. The #41 project ID ledger opens in WAL mode ([`manifests.py`](../../src/cmw/jobs/manifests.py)). | The SQLite documentation states that WAL does not work over a network filesystem. Rollback-journal locking on that filesystem is unknown. |
| Admission and telemetry | Admission, Bounded Sharing, the external-activity guard ([`activity.project`](../../src/cmw/jobs/activity.py)) and CPU/RAM telemetry are host-local. | PBS owns admission and placement. Jobs are routed by resources, and a queue must not be named. Walltime has a site maximum. |
| Engines | VASP runs through the D31 port runner (`run-vasp.sh`). | Engines come from environment modules. No runner profile exists for the cluster. |
| Reachability | [`contracts.NodeState`](../../src/cmw/nodes/contracts.py) has `REACHABLE`, `UNREACHABLE`, `AUTH_FAILED` and `INCOMPATIBLE`. Polling is every 5 s with a 15 s staleness threshold (D11). | Every new connection would need a password unless a session is reused. |

The existing frontend, protocol 1, JSON-lines endpoint
([`endpoint.serve`](../../src/cmw/nodes/endpoint.py)), held submission, transfer,
request receipts, fetch, review and portal do not depend on these assumptions.
They are reused unchanged.

## 2. Decisions

| ID | Decision |
| --- | --- |
| S01 | Add node kind `pbs` to `cmw nodes`. Its queue is backed by PBS, which replaces the CMW controller and dispatcher on that node only. The node is always an explicit target, with no pooled scheduling or automatic node choice across the Macs and the cluster. Existing node kinds and local Jobs behaviour are unchanged. |
| S02 | Add connection auth mode `session`. The user opens an OpenSSH master themselves and types the password there. CMW attaches with `ControlPath=<enrolled socket>`, `ControlMaster=no` and `BatchMode=yes`. It never creates a master, never prompts, and never reads, stores or forwards a credential. The enrolled socket must be an absolute path in a directory owned by the user with mode 0700. Key-authenticated connections keep their current argv. |
| S03 | A missing or expired master is a new immediate portal state, `AUTH_REQUIRED`, distinct from `AUTH_FAILED`. Before each connection attempt, the frontend checks the master with `ssh -O check` on the enrolled socket. If the check fails, the frontend makes no connection attempt and shows the one command the user runs. If the master disappears between the check and the attempt, `BatchMode` makes the attempt fail without a prompt. That failure is reported as `AUTH_REQUIRED` when a repeated check also fails, and as `AUTH_FAILED` otherwise. `AUTH_REQUIRED` resets the transport-failure count and never counts toward the D10 three-failure rule. It is a frontend state, not a protocol-1 error code. |
| S04 | A connection pins one login node by its `/etc/machine-id`, checked on every reply as for any Linux node. CMW cannot check the SSH host key itself, because ssh does no key exchange when it attaches to a master (F11). The owner accepted on 2026-11-20 that the host key is verified only by the user's own ssh, against their `known_hosts`, when they open the master. With an explicit control socket the master, not the host name CMW passes, decides which node is reached. An endpoint reached on another login node refuses, because its machine-id differs from the enrolled host; moving to another login node is a reviewed re-enrollment. Machine-id uniqueness across login nodes is still unconfirmed (F06). |
| S05 | The endpoint stays a foreground process on the SSH stream and exits when the frontend disconnects. Nothing CMW-owned persists on a login node: no daemon, cron job, controller, `nohup` process or pilot job. A `pbs` lane polls no faster than every 30 s. Its staleness threshold is three poll intervals, keeping the D11 ratio. Each status makes two bounded calls: `qselect -x -u` for the user's job IDs, then `qstat -x -f -F json` for those IDs, because `qstat` ignored `-f -F json` when `-u` was given on the probed server (F02). Phase 1 fixes the exact interval after fact F16 has measured what a `qstat` call costs. |
| S06 | The CMW runtime is deployed as an immutable snapshot under the user's home directory, with a source-commit record and a SHA-256 manifest, following the D31 pattern. Its interpreter is pinned (D30) to a module or Miniforge Python of at least 3.11 (fact F10). The registered endpoint executable is an `-I -B` wrapper. Compute nodes run the same snapshot through the shared filesystem. |
| S07 | A `SchedulerBackend` sits beside [`LocalBackend`](../../src/cmw/nodes/backend.py), with the same `call(operation, params, target)` surface and schema-1 observations. `capabilities` advertises the operations the node cannot perform. Suspend, continue, dispatch, move, sharing and controller start, stop, pause and resume are refused with the existing `UNSUPPORTED_OPERATION` code, and the portal hides those controls for the lane. Python owns state mapping, PBS script values, receipts and reconciliation. The read-only `qselect` and `qstat` calls run from Python with a fixed argv and enrolled absolute paths, like the existing `findmnt` and `diskutil` probes; a non-login shell on the probed cluster did not have the PBS clients on its PATH (F02). From Phase 2 a small shell helper owns module loading and `qsub`/`qdel`. It takes a fixed argv and returns JSON with stable exit codes (AGENTS §11). |
| S08 | CMW keeps Hold records, request receipts, attempt bindings and PBS job IDs in a private state root under the user's home directory with mode 0700. Only the endpoint on the pinned login node opens it. Compute nodes never open it. The store is the existing SQLite `Store` in rollback-journal mode only if fact F09 shows that POSIX locks, `fsync` and atomic rename behave correctly on that filesystem. Otherwise it is one atomically renamed JSON record per attempt, like the #41 manifest sidecars. Phase 2 records the choice and its evidence. |
| S09 | Storage for this node kind uses network-filesystem identity: filesystem type, source, mount point, and the stable identifier the filesystem supplies. On the probed cluster `findmnt` reported no UUID for its GPFS filesystem, so that identifier is the `statfs` filesystem ID (F08). The local-disk rule in `storage.platform_volume` is unchanged for existing nodes. Storage on an auto-deleting scratch filesystem is opt-in per enrollment, and its deletion period is shown as a warning wherever that storage is selected. |
| S10 | Release is submission. `submit` is unchanged: preview, transfer, verify, then register on Hold. A held job consumes nothing on the cluster. `release` keeps its preview and confirmation. It renders a fixed, versioned job-script template from the typed resources as `select=1:ncpus=…:mpiprocs=…:mem=…` and `walltime=…`, names no queue, and calls `qsub`. Walltime is a required typed resource for `pbs` nodes, with no default (as D16 does for cancel mode), and values over the confirmed maximum (fact F05) are refused at preview. CMW always sends `ncpus`, `mem` and `walltime` explicitly, because the server fills missing values with its own defaults (F05). Jobs are submitted not rerunnable (`-r n`), because the server default is rerunnable; a `run_count` above 1 is a contradiction that leaves the attempt `Unknown`. No user-supplied text is interpolated into shell. |
| S11 | Each PBS submission is made at most once automatically. The release intent is committed before `qsub` is called. The PBS job name is derived from the attempt UUID, and the job's output and error paths are set inside that attempt's directory. A lost reply is resolved by looking the job up by name and then checking that its `Output_Path` names the attempt directory, never by submitting again. The probed server did not show `Variable_List` in `qstat` output (F02), so it cannot carry the identity. If neither current nor historical PBS records match, the attempt becomes `Unknown` and needs explicit review. CMW never resubmits it. |
| S12 | The job script runs the existing `payload.sh` on the compute node under a job-side guardian from the same snapshot. The guardian holds the D34 ownership evidence locally: compute host, boot session, PID, start token and `PBS_JOBID`. It writes that evidence and the payload exit receipt atomically into the attempt directory on the shared filesystem. It never opens the queue store. Phase 2 factors the GO/RELEASE/drain part of `runtime.worker` so that the local worker and the job-side guardian share one implementation, rather than adding a second supervisor. |
| S13 | The endpoint maps states as in [§3](#3-state-mapping), from PBS records and receipts. It reconciles every non-terminal attempt at each handshake, because nothing observes PBS while no frontend is connected. Ingesting a receipt is an ordinary store transition, so the store then publishes `state.json` as it already does ([`manifest_state.py`](../../src/cmw/jobs/manifest_state.py)). The job script never writes `state.json`. |
| S14 | Cancel is `qdel`, with the existing preview, confirmation and request receipt. It is the D13 `hard` mode only. STOPCAR `graceful` and `soft` modes come later. Cancelling a job still on Hold stays a store-only change. The cancel request is committed before `qdel`. A cancelled attempt never becomes `Done` (D13), even if a receipt with exit code 0 appears later. PBS's own grace between signals (fact F05) replaces the D14 10 s grace on this node, and its value is shown. |
| S15 | Progress, file listings, log tails and review reuse the existing owner-side readers on the login node against the shared filesystem. Resource use is PBS `resources_used`, labelled as scheduler-reported. Host CPU/RAM, thermal, admission and sharing lanes do not apply; the lane shows PBS queued and running counts instead. The lane lists only the user's own jobs (`qstat -u`). The probed server let users query other users' jobs (F05), and CMW never shows them. PBS jobs not submitted by CMW appear as read-only external rows, like the existing `E…` observations. They are never adopted, signalled or cancelled. |
| S16 | Remote submissions to a `pbs` node accept typed profiles only (D23). Password-only login leaves no `authorized_keys` entry for the D05 forced-command gate, so the endpoint enforces this itself. The first real profile is a cluster VASP profile. It pins the module and binary, verifies NCORE/KPAR against the INCAR without rewriting it (D17–D19), and keeps `potcar_authorized` (D07, D21). Restart artifacts stay on the cluster; same-queue `--restart-from` parents already avoid moving WAVECAR. |

### Relation to D01–D34 for `pbs` nodes

| Decisions | For a `pbs` node |
| --- | --- |
| D01, D04, D07, D08, D09, D15–D17, D21–D23, D25, D26, D30, D32 | Apply unchanged. D32 means Phase 2's probe job uses a sandbox namespace, not the production queue. |
| D19 | The NPAR, KPAR and NCORE rules apply unchanged. The ranks × threads bound is the requested `ncpus`, not the login node's physical cores. |
| D02 | One node enrollment and one queue (`main`), as for the Macs. Storage may need more than one enrollment if open question 2 selects both the home and the scratch filesystem. |
| D05 | Not applicable: there is no key authentication on which to place a forced command. S16 moves D23 enforcement into the endpoint. |
| D06 | Applies when P3b lands. Until then, transfer uses SFTP through the same master (S02). |
| D10, D11 | Extended by S03 (`AUTH_REQUIRED`) and S05 (per-lane interval and threshold). |
| D12 | Applies to identity and runtime checks. The `qstat` call is bounded separately (S05). |
| D13, D14 | `hard` only (S14). The PBS grace replaces D14. |
| D18, D31 | Runner-specific to the Mac port. The cluster VASP profile (S16) meets the same verify-only rule. |
| D20 | Applies to the login node. Compute-node hardware is PBS-reported, not node policy. |
| D29, D33 | Not applicable. |
| D34 | Enforced by the job-side guardian on the compute node (S12). The endpoint relies on PBS records and receipts. |

### Refinements to the issue design

Checking the issue against the code on `main` changed four points:

1. **`state.json` is published by the queue owner, not by the job script.**
   It is a projection of the committed queue record
   ([`manifest_state.py`](../../src/cmw/jobs/manifest_state.py)), so it is
   published when the endpoint ingests the receipt (S13).
2. **`payload.sh` needs a guardian.** It waits for `GO` after ownership is
   persisted and for `RELEASE` after drainage. The compute node therefore runs a
   job-side guardian built from the existing worker code (S12).
3. **The D05 forced-command gate cannot be installed on the cluster**, because
   it relies on key authentication. D23 enforcement moves into the endpoint
   (S16).
4. **The #41 ID ledger must stay on a local disk.** It opens in WAL mode, which
   SQLite does not support on network filesystems. The owner frontend keeps
   allocating IDs from its own local ledger. The cluster never hosts a project
   ledger (open question 5).

## 3. State mapping

| CMW state | Evidence |
| --- | --- |
| `Hold` | Registered, not submitted. |
| `Queue` | PBS reports the job queued, held or waiting. |
| `Run` | PBS reports it running, or reports it running after a receipt has appeared (still draining). |
| `Done` | Payload exit receipt with exit code 0 and no cancel request. |
| `Fail` | Payload exit receipt with a nonzero exit code. Or PBS reports the job finished without a receipt (walltime, node failure), with the PBS exit status and comment recorded verbatim. |
| `Cancelled` | A committed cancel request, and PBS reports the job finished. The receipt, if any, is retained but does not make the job `Done`. |
| `Unknown` | Neither a matching PBS record nor a receipt, for example after PBS history has expired, or a lost `qsub` reply that matched nothing (S11). Also a job PBS ran more than once (S10). |

`Done` remains operational completion, not scientific success; `scf_result`
still comes from the classifier. `Unknown` is never forced to `Done`, and it
blocks nothing else on the node. PBS keeps finished jobs only for the server's
`job_history_duration` (F03), so a job that finishes while no frontend connects
for that long can only be classified from its receipt. A job that PBS ended
before it ever started has no `Exit_status`, only a comment (F15), and maps to
`Fail`. A receipt on an auto-deleting scratch filesystem can be deleted before
the next connection, so such an attempt can end as `Unknown`.

## 4. Phases

Each phase is one PR, separately authorized, and updates
[`CAPABILITY_MAP.md`](../../CAPABILITY_MAP.md).

| Phase | Content | Contacts the cluster |
| --- | --- | --- |
| 0 | This design and the fact probe checklist. No code. | No. The probe runs in its own authorized window. |
| 1 | In two PRs. 1a: `pbs` enrollment without storage and the read-only endpoint backend. 1b: S02–S05 session transport and the read-only lane: `cmw nodes connect/check`, `jobs --node ALIAS status`, and the cluster's lane in `watch --all-nodes` listing the user's PBS jobs. Tests use synthetic `ssh` and `qstat` fixtures on both CI platforms. | Only for qualification, in an authorized window. |
| 2 | S08/S09 store and storage choice (after F09 and open question 2). S10–S14: held submission and release through `qsub`, the job-side guardian, receipts, lost-reply recovery, `qdel` cancel and walltime classification against a fake PBS. Then one authorized one-core, one-minute probe job in a sandbox namespace, which also confirms fact F14. | One authorized job. |
| 3 | S16 cluster VASP profile, manifests with `state.json` and `scf_result`, progress, review and fetch. | One authorized small fixture calculation. |
| 4 | Later: on-cluster successor chains, STOPCAR cancel modes, walltime-aware stop, an ORCA profile, a second scheduler to test the adapter boundary. | — |

## 5. Fact probe checklist

The probe runs only inside an explicitly authorized window. The user opens the
SSH session themselves (S02). No password, private key, licence file or licensed
content is recorded, and nothing is submitted to PBS. Items F01–F08, F10 and
F12–F16 only read. F09 and F11 write, and only into one disposable directory
under the home directory that is removed afterwards; they need their own
approval within the window. F14 needs a compute node and is deferred to Phase 2.

| ID | Fact | Read-only method | Decides |
| --- | --- | --- | --- |
| F01 | PBS Pro version | `qstat --version` | S07, S11 |
| F02 | JSON output | `qstat -f -F json -u "$USER"`: whether it is accepted, schema keys, output size | S07, S13 |
| F03 | Job history | `qstat -x -u "$USER"`; `qstat -B -f` for `job_history_enable` and `job_history_duration` | S11, S13, §3 `Unknown` |
| F04 | Job-name limits | `man qsub` (`-N`): maximum length and allowed characters | S11 |
| F05 | Limits and routing | `qstat -Q -f`: `resources_max.walltime`, routing, `kill_delay`; required resource keys | S10, S14 |
| F06 | Login nodes | On each reachable login node: `hostname -f` and `cat /etc/machine-id`; the host-key fingerprint from the local `known_hosts`. Confirm machine-ids differ. | S04 |
| F07 | Login-node policy | `ulimit -a`; `cat /proc/self/cgroup`; the site policy on long-running login processes | S05 |
| F08 | Filesystems | `command -v findmnt`; `findmnt --json --target "$HOME" --output TARGET,SOURCE,FSTYPE,UUID,OPTIONS` and the same for the scratch filesystem; `stat -f -c '%T %i' "$HOME"` | S08, S09 |
| F09 | Locking and rename (writes) | In the disposable directory: whether an `fcntl` lock held on one login node is visible from another; whether a rename is visible atomically from another node; whether SQLite can create and reopen a rollback-journal database | S04, S08 |
| F10 | Python | `module avail python`; `module avail miniforge`; for a candidate interpreter, `python3 -c 'import sys, sqlite3; print(sys.version, sqlite3.sqlite_version)'` | S06 |
| F11 | SSH session (writes the socket) | The user runs `ssh -M -S ~/.ssh/cmw-pbs.sock -fN <login node>`. Then: `ssh -S ~/.ssh/cmw-pbs.sock -O check <login node>`; a second command through the master; `sftp -o ControlPath=~/.ssh/cmw-pbs.sock`; how long the master survives idle and across a VPN reconnect | S02, S03, open question 1 |
| F12 | Clock | `date -u +%s` on the login node against the frontend (display only, D25) | D25 |
| F13 | VASP | `module avail vasp`; `module show` for candidate versions; `id -Gn` for the licence group. VASP is not run. | S16, open question 4 |
| F14 | Compute-node view (Phase 2) | From the authorized probe job: the same home path, compute `/etc/machine-id`, boot-session evidence, `PBS_JOBID` | S12 |
| F15 | Exit-status conventions | `qstat -x -f -F json <id>` for one of the user's own finished jobs still in history: `Exit_status`, `comment`, `resources_used` | §3, S15 |
| F16 | `qstat` cost | Wall time of `qstat -f -F json -u "$USER"`, measured three times | S05 |

## 6. Probe results

Run on 2026-11-20 against one login node through a user-opened master. The
checks only read, except that the `qstat` timing wrote two temporary files to
the login node's `/tmp` and removed them at once. Each timing is a single
sample. The site-specific values (host, server and queue names, versions,
module names, paths and per-user limits) are kept in the owner's local notes.
This table records only what bears on the decisions.

| ID | Finding | Effect |
| --- | --- | --- |
| F01 | PBS Pro 2026.1, with two servers. Job IDs carry the server name as a suffix. | S07, S11: job IDs are opaque strings including the suffix. |
| F02 | `-F json` is accepted. Job records include `job_state`, `substate`, `comment`, `Exit_status` (finished), `Job_Name`, `Output_Path`, `Error_Path`, `Submit_arguments`, `Resource_List`, `resources_used`, `run_count`, `Rerunable`, `queue`, `exec_host`, `stime` and `obittime`. `Variable_List` is absent from JSON and text output. `qstat -f -F json -u` prints the text table instead; `qselect -x -u` lists the user's current and history-retained IDs, which `qstat -x -f -F json <ids>` accepts. A non-login shell's PATH lacks the PBS clients. | Changes S11 (identity via `Output_Path`), S05 (two calls) and S07 (absolute paths). |
| F03 | Job history is enabled, with a `job_history_duration` of 48 h. `qstat -x -u` lists finished jobs. | S13, §3. |
| F04 | `man qsub` gives the `-N` format only as "Job Name", with no length. | S11: confirm the limit with the Phase 2 probe job. |
| F05 | The default queue is a routing queue; maximum walltime depends on the destination queue. The server supplies default CPUs, memory and walltime for missing values. Per-user limits on queued jobs and running CPUs exist. Users may query other users' jobs. `kill_delay` was not visible. | S10, S15; open question 3. S14: the grace period is still unconfirmed. |
| F06 | Linux, with a valid 32-hex `/etc/machine-id`. Only one login node was reached. | S04: uniqueness across login nodes is unconfirmed. |
| F07 | Shell CPU, memory and process limits are unlimited. The session runs in a systemd user slice with CPU and memory cgroups; their limits were not read. The site policy on long-running login processes was not read in full. | S05. |
| F08 | The home and scratch filesystems are on the same GPFS mount. `findmnt` reports no UUID; `statfs` reports type `gpfs` and a nonzero filesystem ID. | S09 (identifier), S08. |
| F09 | Not run (writes). | S08 stays open until it runs, before Phase 2. |
| F10 | The system Python is below the 3.11 minimum. Module and Miniforge interpreters of 3.11 and later are available. | S06: pin a module or Miniforge interpreter, never the system Python. |
| F11 | Partial. The user-opened master worked, and several commands ran through it with `ControlMaster=no` and `BatchMode=yes`. Through the master, `ssh -v` shows `mux_client_request_session` and no server host key. SFTP, idle lifetime and reconnect were not tested. | S02, S03, S04. |
| F12 | Remote and local UTC epochs agreed to within the 1 s resolution of the command round trip. | D25. |
| F13 | Several VASP modules are available. No group name contains `vasp`, so how the licence is enforced is unknown. VASP was not run. | S16; open question 4. |
| F14 | Deferred to the Phase 2 probe job. | S12. |
| F15 | Finished jobs that ran have `Exit_status` 0, `substate` 92 and a comment ending "and finished". A job ended before starting had no `Exit_status`, `substate` 91, and a comment beginning "Not Running: … and terminated". `resources_used` keys are `cpupercent`, `cput`, `mem`, `ncpus`, `ngpus`, `vmem` and `walltime`. | §3, S15. |
| F16 | `qstat -u` took 0.09 s, `qstat -B -f -F json` 0.30 s, `qstat -f -F json` for three jobs 0.49 s, and both `qselect -u` calls together 0.10 s. | S05: a 30 s poll is ample. |

## 7. Open questions for the owner

These need an owner decision before the phase named. Phase 0 does not decide
them.

1. **Long-lived SSH master.** Accepted by the owner on 2026-11-20. Whether the
   site permits a long-lived master is still observed through F11.
2. **Job directory location (Phase 2).** The home filesystem, a group project
   allocation, or the auto-deleting scratch filesystem? Deferred by the owner on
   2026-11-20 to Phase 2, before the first submission; Phase 1 enrolls no
   storage. This sets the storage enrollments (S09, D02).
3. **Submission cap (Phase 2).** Should CMW cap how many jobs it has submitted
   to PBS at once, or leave ordering entirely to PBS?
4. **First engine (Phase 3).** VASP or ORCA? Is VASP available on the cluster
   under the group's licence? Fact F13 informs the answer.
5. **Frontends (Phase 2).** May only the MacBook submit to the cluster, or the
   Mini as well? This interacts with the #41 rule that the owner frontend
   allocates job IDs from its local ledger.
