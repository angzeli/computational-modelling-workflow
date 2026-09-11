# CMW Jobs: local execution and bounded sharing

CMW Jobs runs already-prepared, trusted foreground commands in an explicit order.
Sequential mode is the default: exactly one managed execution may own the slot.
Explicit Bounded Sharing permits one primary plus at most one managed auxiliary;
primaries never overlap. External programs started outside Jobs remain outside
CMW execution ownership. This is a local POSIX feature;
macOS Apple Silicon is the acceptance platform for this implementation. Linux and
other platforms have not been validated. Windows execution is not supported.

Install the existing checkout, including the optional console:

```sh
python3 -m pip install -e '.[jobs]'
cmw jobs status
```

Without the `jobs` extra, non-interactive commands still work; `watch` reports the
missing optional dependency. Importing `cmw` and using existing commands do not
import Textual or initialize a UI. psutil supplies process birth identity.

## Prepared jobs and controls

```sh
cmw jobs add --name prepared-SP --engine ORCA --cwd '/path/to/prepared case' \
  --cpus 8 --mpi-ranks 8 --threads-per-rank 1 --memory-gib 20 \
  --env PYTHON_BIN=/path/to/python \
  -- /bin/bash '/path/to/prepared case/run.sh'
cmw jobs start
cmw jobs watch
cmw jobs status --json
cmw jobs show J1.1
cmw jobs logs J1.1 --stream stderr --bytes 16384
cmw jobs pause
cmw jobs hold J2.1
cmw jobs move J3.1 1
cmw jobs release J2.1
cmw jobs resume
cmw jobs cancel J3.1
cmw jobs cancel J1.1 --yes
cmw jobs stop
```

IDs above are illustrative; use IDs returned by `add`. Options for `add` precede
`--`; the remainder is an explicit argv array, not a shell command string.
Relative or empty `PATH` entries are evaluated from the job working directory;
internal receipt/ownership helpers do not depend on the payload search path.
`--state DIRECTORY` goes immediately after `jobs`, before the operation. The
`CMW_JOBS_STATE` environment variable applies the same isolation to all clients.

`add` only enqueues. `status`, `show`, `logs` and `watch` never start or resume
execution. `start` explicitly starts a detached controller and enables dispatch
unless Unknown/Cancelling state requires attention. A second start is rejected;
use `resume` on an existing controller. `stop` requests controller shutdown and
pauses dispatch while the independent execution supervisor finishes an active
job. After shutdown, `start` reconciles that supervisor before dispatching.

`pause` stops future dispatch and does not pause/kill the active job. Pending
cancellation does not disturb the active job. Running cancellation requires
`--yes` (or typing `CANCEL` in the console), transitions through Cancelling, then
pauses dispatch until explicit resume after termination is confirmed. TERM is
sent first, with KILL after a two-second grace period if required.

For a trusted MPI launcher that retains CMW's dedicated session but creates
separate rank process groups, opt in with `--env CMW_JOBS_OWN_SESSION=1`.
CMW then observes all live session members and signals verified subgroups before
its pinned leader. Completion waits for the session to drain; telemetry and the
external-workload guard use the same scope. Programs must not detach into new
sessions. Default jobs retain process-group-only ownership. This opt-in has been
tested locally with Open MPI 5.0.9; it does not validate scientific results.

Hold and order apply only to jobs that have not started. A held or blocked queue
head blocks later jobs. Move it explicitly, release it, fix its launch blocker,
or cancel that pending job. The persistent ORDER is independent of job ID and
screen row order. The console keeps rows stable while ORDER changes.

Console keys: Enter details, L logs, P toggle dispatch, H hold/release, O order,
X cancellation confirmation, B read-only scheduling details, Q or Ctrl-C detach. Arrow/Page keys scroll. Narrow
layouts prioritize ID, status/engine, current CPU/RSS and elapsed/process age;
order, requested CPUs, names, engine and reason remain available in details. Very small
terminals can scroll the table horizontally. Details and logs have scrollable
panels. Live log tails are bounded to 16 KiB per stream and unchanged logs are
not reread. Control sequences are stripped and UI markup is rendered literally.

## Status, resources and failure policy

| Status | Meaning |
| --- | --- |
| Queue | Pending in the persistent dispatch order; REASON explains the wait |
| Hold | Pending and explicitly held by the user |
| Starting | Launch intent committed; the slot is reserved before process launch |
| Run | The execution supervisor has persisted ownership and admitted execution |
| Cancelling | Cancellation requested; termination has not yet been confirmed |
| Done | Foreground execution and its managed process group completed with exit 0 |
| Fail | A concrete launch/execution failure, with captured exit evidence |
| Cancelled | Cancelled before start or managed group termination confirmed |
| Unknown | Ownership/completion cannot be safely established; progression stops |

Done does **not** establish scientific validity, geometry convergence or energy
quality. CHECK is hidden from the main table and details say Not evaluated.
Existing runner validation still runs; Jobs does not copy unrelated check results
or introduce new scientific gates.

Default `--on-failure pause` stops later dispatch after Fail. Explicit
`--on-failure continue` allows independent subsequent commands to run after a
confirmed failure. It never bypasses Unknown or unconfirmed cancellation.
A missing executable or cwd remains a pending launch blocker, regardless of policy.

CPU slots, MPI ranks, threads/rank and memory are separate **requested** fields.
They are not measured RSS, hardware limits, reservations, VASP NCORE, or settings
injected into input files. Missing resource data is displayed as a dash. Header
resource totals describe active/uncertain attempts only. ELAPSED excludes queue
waiting time and is fixed after completion; hours can exceed 24. Controller
heartbeat age and stale/offline state are displayed independently of job state.

## Current CPU and RAM usage

The machine line describes the whole local Mac: CPU is a percentage from changes
in host CPU-time counters. RAM shows used / total in GiB, with used defined as
`total - available`. This is psutil's platform accounting, not a claim of byte-for-byte agreement
with macOS Activity Monitor. It is independent of the visible workload totals.

CPU NOW displays **100% per fully busy logical CPU**: summed process CPU-time
deltas divided by monotonic elapsed time, multiplied by 100. `710%` means roughly
7.1 logical CPUs' worth of CPU time over the sample interval. Workload CPU can
exceed 100%; it is not capped by requested CPUs or NPROC. The machine header
retains the whole-host 0–100% scale. JSON retains raw `cpu_cores` for compatibility.
The first observation needs a baseline and shows `…` (warming up), not fake zero.
A sampling gap longer than five seconds also starts a fresh CPU baseline.
The console retains its sampler and updates in the existing two-second background
observation cycle. One-shot `status`/`show` starts a new sampler, so CPU ordinarily
warms up there while available RAM is reported immediately.

RAM NOW is current aggregate RSS from verified live members. **Summing RSS can
count shared pages more than once**; it is not exact physical memory ownership or
reserved RAM. Requested CPU slots, MPI ranks, threads/rank and memory remain
unchanged in job declarations and details. External requested resources remain
unknown. Pending and terminal jobs show no current workload usage; Done does not
keep a previous Run measurement labelled current.

The table uses `*` for a partial measurement, `~` for stale values, `?` for an
unavailable cached value and `—` when no value is available. Details show CPU and
RAM quality separately, measurement age, and measured/total member counts. New
members need their own CPU baseline; a partial RSS subtotal remains visible when
some relevant members are inaccessible. Samples older than five seconds are
stale. Sampling runs outside rendering and failures retain distinguishable
last-known data without changing execution state.

`cmw jobs status --json` adds `machine_usage`, plus `usage` on each active managed
job and external observation. Numbers remain raw CPU percentages/core-equivalents
and bytes, accompanied by timestamps, age, stale, quality, member counts and
reason. `cmw jobs show <JOB_OR_OBS_ID>` uses the same projection. No telemetry is
stored as database history or emitted as per-sample queue events.

Telemetry is advisory: zero CPU does not mean a sleeping/stopped computation has
finished, unavailable RSS does not invalidate an external observation, and low
machine CPU alone cannot bypass BUSY. In explicitly enabled Bounded Sharing,
fresh host telemetry adds admission vetoes after identity, declarations and
commitment checks; it never releases an existing commitment. Existing ownership, guard, cancellation and
completion evidence remain authoritative. There are no peaks, history, resource
limits, automatic tuning, arbitrary parallel scheduling, IO/GPU/temperature metrics or
ETA. The supported runtime boundary remains local macOS with the locked psutil
dependency; tests use deterministic samples and owned benign processes.

## Integration and foreground command contract

Use the existing prepared CMW shell command, including its existing arguments,
execution profile environment, runtime contract, layout, preflight and
finalization. Jobs determines **when** to invoke it; those runners still determine
scientific intent, settings, output layout and validation. For example, a HOF
materialized `run.sh` may be passed through `/bin/bash` as above. Existing HOF
queues/authorization remain independent; Jobs does not bypass their guards.

An optional `--layout /path/to/layout.json` resolves an existing v1/v2 CMW layout,
checks that its working directory matches `--cwd`, and records its exact target
and attempt identity. It does not prepare inputs or replace runner checks. The
queue's own attempt UUID identifies the operational invocation, separately from
the referenced scientific attempt. A new enqueue creates a new operational
attempt; no existing attempt/log is overwritten and there is no retry command.

Commands must remain in the controlled foreground process group until their work
finishes and propagate payload failure through their exit status. Scripts should
use explicit interpreter/script argv. Bash/zsh receive `-e -o pipefail`; POSIX sh
receives `-e`. This prevents an ordinary failed command followed by a final echo
from silently reporting success. Scripts that deliberately suppress failures,
reset these options, hide errors inside language runtimes, or implement their own
success policy remain responsible for propagating failure. Jobs cannot recover
exit codes that an arbitrary wrapper deliberately discards.

A pinned shell group leader remains alive while same-group children drain, even
if the outer command exits first. Cancellation targets that verified group,
including its children, rather than only the outer script. Arbitrary daemonizing
commands, `setsid`/process-group escape, detached scheduler/MPI workers, remote
launches, and unowned external processes are unsupported. Do not enqueue wrappers
that return before detached computations finish. No name-based process takeover
or machine-wide termination is performed.

Computational stdin is `/dev/null`; interactive engine prompts are unsupported.
Payload stdout/stderr go to attempt log files, not console pipes. The preserved
shell wait status is recorded as `exit_code`; values above 128 can represent a
signal or an explicit program exit and are not guessed into a signal identity.
A signal is recorded separately when the supervisor knows it sent that final
termination signal. Engine-specific output files remain where the runner writes them.

## Persistence, lifecycle and recovery

Default state is `~/Library/Application Support/cmw/jobs` on macOS; on Linux it
would be `$XDG_STATE_HOME/cmw/jobs` or `~/.local/state/cmw/jobs`. It is independent
of the current directory. Use a stable local filesystem, not network/removable
storage. Explicit isolated state is supported for tests/demos.

- `queue.sqlite3`: transactional queue order, controls, jobs and bounded-query events.
- `controller.lock`, `start.lock`: kernel ownership locks, never PID-only takeover.
- `controller.log`: controller/supervisor lifecycle diagnostics.
- `attempts/<UUID>/`: stdout/stderr, worker lock and atomic payload exit receipt.

The state directory is created with private permissions; environment overrides
and exact argv are persisted there and visible in job details. No scientific
inputs, geometries, scripts, binaries, restart files, outputs or external data
are snapshotted or guaranteed immutable. No directory checksums, file copies,
source receipts, data handoffs, automatic cleanup or input rewrites occur.
Enqueueing does not freeze the scientific working directory.

The controller starts a detached session with independent descriptors. Each
execution has an independent supervisor and payload group. Q, Ctrl-C, closing
watch, or closing the original start terminal leaves authorized execution and
subsequent dispatch alive during the current logged-in OS session. No LaunchAgent,
service, login item or shell startup hook is installed. Logout, reboot, shutdown,
crash and power loss are outside the survival guarantee.

Restart acquires the controller kernel lock and reconciles active attempts by
worker lock, host/boot/PID birth identity and persisted launch claims. A live
verified supervisor remains responsible for completion; the same attempt is not
relaunched. The supervisor records completion only after exit evidence and
process-group drainage, with a final kernel check that the group is absent after
its leader is reaped. A process-enumeration race therefore cannot release the
queue early. Missing PID/log text/files alone never mean success.
A crash near spawn without sufficient ownership metadata becomes Unknown after
a short admission grace period and blocks the queue. A lost supervisor can also
leave a pinned shell or a computation alive; Jobs will not signal unverified
ownership. Inspect details/controller logs and resolve the underlying process
situation manually. There is deliberately no “force Done”, blind retry, or
unsafe override for Unknown. In particular, do not erase state to resume a queue
while an old execution might still be alive.

This is conservative recognition of existing managed execution, not exactly-once
execution across arbitrary crashes and not scientific checkpoint/restart support.

## Safe reproducible demo

From an installed checkout, this creates only synthetic commands in a fresh
local temporary directory. Save the printed directory if revisiting the demo.
Use a new terminal to verify controller survival after closing the original one.

```sh
cmw_demo_dir=$(mktemp -d -t cmw-jobs-demo)
export CMW_JOBS_STATE="$cmw_demo_dir/state"
printf 'Demo state: %s\n' "$CMW_JOBS_STATE"
cmw jobs add --name A --engine Synthetic --cwd "$cmw_demo_dir" --cpus 1 -- \
  /bin/bash -c 'printf "A starts\n"; sleep 3; printf "A ends\n"'
cmw jobs add --name B --engine Synthetic --cwd "$cmw_demo_dir" --cpus 1 -- \
  /bin/bash -c 'printf "B starts\n"; sleep 2; printf "B ends\n"'
cmw jobs add --name C --engine Synthetic --cwd "$cmw_demo_dir" --hold -- \
  /bin/echo 'C released'
cmw jobs status
cmw jobs start
cmw jobs watch
# Q detaches. These commands use the same isolated state:
cmw jobs release J3.1
cmw jobs status --json
cmw jobs logs J1.1
# Once jobs finish, stop the demo controller. Retain logs for inspection.
cmw jobs stop
unset CMW_JOBS_STATE
```

Automated acceptance is in `tests/jobs`: real subprocess ordering/cancellation,
controller crash/restart, same-group descendants, actual PTY terminal closure,
console keys/resize/selection, bounded safe logs and a queued existing CMW
workflow with fake ORCA. Tests require local process-identity and terminal access;
a restrictive sandbox may deny macOS sysctl/PTY operations. All test queues and
processes are isolated. See the [implementation record](architecture/cmw-jobs-implementation.md)
for measured validation results and compatibility boundaries.

## External activity and admission guard

`cmw jobs status`, `cmw jobs status --json` and `cmw jobs watch` observe external
activity even with no managed jobs and an offline controller. Starting or resuming
is not needed to observe. External observations have `E...` identifiers and a
separate read-only table; `cmw jobs show E...` displays current evidence. They are
never inserted into the managed `jobs` array. External logs, cancel, hold, order,
retry, adoption and scientific checks are unavailable. An observation disappearing
means **no longer observed**, not Done, Fail or scientific completion.

Default scope is the current local user and accessible local process metadata,
validated here on macOS Apple Silicon. The recognized OS executable basenames
are `vasp_std`, `vasp_gam`, `vasp_ncl` and the ORCA main executable `orca`.
An editor, shell, Python analysis script, `mpirun` or `caffeinate` does not match
merely because its arguments mention an engine. No `orca_*` wildcard is used.
Arbitrarily renamed engines, independent workers with other names, other users,
containers and remote machines are outside the recognition guarantee.

For a differently named executable, an optional `external-activity.json` in the
selected Jobs state directory accepts only an additive exact-path mapping:

```json
{"executables": {"/absolute/path/to/custom-engine": "VASP"}}
```

Values are `VASP` or `ORCA`; paths are resolved without executing any hooks.
Defaults cannot be disabled. Invalid/unreadable configuration blocks admission.
There is no force option or per-PID ignore list. Existing resource profiles do
not provide a global engine executable registry; this small mapping supplies
only that missing recognition information. No process environments, open files,
scientific inputs/logs or full command-line inventories are read.

| Guard state | Meaning and admission effect |
| --- | --- |
| NO_MATCH | No recognized external computation observed in a usable fresh scan; remaining controller, user intent, order, ownership and preflight gates still apply |
| BUSY | Recognized live external computation observed; blocks Sequential dispatch; only a valid explicit coexistence reservation can permit an Auxiliary under Bounded Sharing |
| UNCERTAIN | Material identity, executable, ownership or intended-scope coverage gap; block new dispatch |
| UNAVAILABLE | Collection/configuration error, timeout or unusable stale evidence; block new dispatch |

Evidence, coverage warnings, observation time and source are separate JSON fields
under `external_activity`; `controller_guard` reports the controller/supervisor's
last evaluation, distinct from a newer client scan. `admission` is a client
projection, never a launch token. Existing managed JSON fields keep their
meanings. Missing optional cwd is a missing detail. When an OS executable path is unavailable but the process name is not a
recognized engine or configured executable basename, report a non-blocking scope
limitation. This is not a per-PID exemption or proof of executable identity.
Recognized names with unresolved executable/identity evidence remain UNCERTAIN;
missing user-scope/name metadata and scan failures still fail closed.
Verified other-user processes are excluded before further inspection. Sleeping
and stopped engines still block; verified zombies alone do not, and surviving
workers are evaluated independently.

The collector uses fresh process objects and OS executable evidence rather than
psutil's shared `process_iter` cache or its argv-based executable fallback. Each
scan has a one-second budget and at most one in-flight daemon collector per
observer. A timed-out collector never authorizes launch; no unbounded retry
threads are created. Stale last-known observations remain display evidence only.
Clients refresh observations modestly in a background worker; the controller
scans independently of clients and checks again in the supervisor before payload
GO. Scans run outside SQLite write transactions; controls and queue order are
revalidated under the existing claim/admission transactions. A caught blocker
leaves/restores the job pending without a payload start or fabricated receipt.
Guard transition events are bounded to the latest 100; full polling inventories
are not appended to event history.

A blocked controller may remain online with dispatch intent ON. The guard does
not turn queued jobs into user-Hold, Fail or Cancelled. Once a fresh scan clears,
dispatch proceeds according to the configured mode only while intent remains enabled and all
other gates pass. Manual pause, held head, failure policy, Unknown ownership and
unconfirmed cancellation retain their existing effects. Resume does not bypass
the guard. External activity appearing during managed execution produces a
conflict warning; neither process is interrupted automatically.

Only verified ownership in the selected queue exempts a managed engine: its
persisted, birth-verified pinned group/session, live supervisor and held worker
lock. Equal names or working directories confer no ownership. Another queue's
process remains external unless this queue proves ownership; no filesystem search
for other state databases is performed.

Grouping is presentation only: direct children of a birth-verified MPI launcher
leading their dedicated session may be grouped. Cwd, names, an interactive shell,
terminal session or bare PGID alone never group computations. Incomplete family
evidence produces separate observations; every surviving recognized process still
blocks independently. NPROC means observed process count, not allocated CPUs or
proven MPI ranks. AGE is the oldest currently observed member's process age, not
an authoritative job start time; it may change when that member disappears.
External requested CPUs/RAM remain unknown.

This is **best-effort conflict avoidance, not machine-wide mutual exclusion**.
No-match does not prove the machine idle. A manual engine can start after a scan
or after a managed payload begins. Independent state directories have no shared
global machine lock. Sequential exclusivity and the explicit primary/auxiliary
limits apply to the selected managed queue only.

An already-running controller retains the code it loaded. To obtain this guard,
use the normal explicit stop/start lifecycle when you choose; installing or
opening a new client does not upgrade that controller. No production controller
was restarted as part of this refinement. An already-open watch client also needs
to be detached and reopened to load new display code.


## Bounded Sharing: one primary and one auxiliary

This mode is explicit admission control, not an OS-enforced CPU/RAM limit. Existing
state and jobs remain Sequential and Primary/exclusive after migration, with IDs,
attempts, order, logs and events preserved. Configuration never starts a controller
or enables dispatch. Returning to Sequential stops future overlaps without
interrupting already-running work.

Legacy schema-1 state remains readable without migration. New mutations migrate
it transactionally to schema 2 only after legacy active attempts have finished
and the original controller is no longer present by owner/lock evidence. The old
running implementation cannot read schema 2 safely: let its attempts finish,
then stop that controller using its original installation before performing new
mutations. CMW does not automatically kill work, stop controllers or restart them
to migrate. These restrictions apply to legacy-state migration, not ordinary
sharing-consent changes in an already-schema-2 queue.

A primary is ordinary production work. Primaries remain strictly sequential.
An auxiliary is explicitly independent work with declared CPU/RAM requirements,
a trusted resource contract and a known write directory. Engine names, expected
duration, low observed CPU and NPROC never imply eligibility. Declared MPI ranks
and threads/rank must not imply more CPUs than the sharing commitment. Only the first
pending auxiliary can bypass waiting primaries during a sharing window; a held
auxiliary blocks later auxiliaries. This is not arbitrary backfill or best-fit
selection. With no primary/shareable external workload, ordinary queue order
applies; an auxiliary may execute alone in that ordinary sequential position.

A managed primary must consent to coexistence. Enqueue with `--allow-auxiliary`,
or change only that consent while it is active in an already-schema-2 queue,
without restarting the primary. Pending-job declarations can be edited through
`sharing`; active resource budgets, role and write scope cannot be rewritten.
An auxiliary that outlives its anchor continues independently,
but the next primary waits until it finishes. At most one auxiliary is active.
Both jobs retain their own supervisors, logs, verified cancellation scope and
completion evidence. Restart reconciles both; failure/cancellation can pause
future dispatch but never automatically kills the other execution. Starting,
Run, Cancelling and Unknown retain commitments; Unknown/Cancelling block new
admission. Only confirmed terminal state releases a managed commitment.

### Policy and declarations

```sh
cmw jobs config
cmw jobs config --mode bounded-sharing --cpu-budget 10 --memory-budget-gib 24 \
  --cpu-reserve 1 --min-available-gib 4
cmw jobs config --mode sequential
cmw jobs sharing J1.1 --allow-auxiliary
cmw jobs sharing J1.1 --revoke-auxiliary
cmw jobs sharing J2.1 --role auxiliary --independent --trust-resources \
  --write-scope /absolute/independent-analysis
cmw jobs status --json
```

Values and IDs above are examples, not defaults or recommended budgets. First
activation requires all four explicit policy values; configuration checks logical
CPU and physical RAM capacity. `--cpus`/`--memory-gib` on `add` remain per-job
requests and must already exist before marking a pending job Auxiliary.
`--trust-resources` records `trusted-declared`: the user vouches for
conservative bounds and a prepared command that respects them. CMW neither proves
arbitrary scientific independence nor silently rewrites inputs, threads, MPI,
NCORE or analysis options. Opaque commands default to exclusive Primary.

Requested, committed and observed resources are separate. Admission requires the
sum of existing commitments plus the auxiliary's declared request to fit both
configured budgets. A current `713%` reading does not reduce an eight-slot
commitment; low RSS does not release committed RAM. Missing CPU or memory
requests prevent sharing. A valid external reservation contributes its declared
CPU and memory budgets, not NPROC or measured RSS.

Sharing also requires two fresh host observations, at least 0.5 seconds apart,
within a five-second window, with the latest no older than two seconds. The
minimum estimated free logical CPU capacity across that window must cover the
auxiliary's CPU request plus the configured reserve. Minimum available RAM must
cover its declared memory plus the safety margin. Available means total minus
currently used as defined by the existing telemetry model. A warming-up, stale
or unavailable window refuses new auxiliary admission. The controller and final
supervisor obtain their own private fresh evidence; opening the console never
authorizes GO. A one-shot `status` or `show` client starts a new sampling window
and may report warming up even while the controller has usable evidence. `watch`
retains its client sampler and needs roughly two observation cycles after CPU
warm-up to populate the two-sample sharing window. Client and controller timing
remain independent; the controller/supervisor always recheck before launch.

A sustained observed CPU overrun across the two samples or a current RSS subtotal
above declared memory produces a warning and stops further admission. RSS may
count shared pages twice, so this is a conservative admission stop, not proof of
physical ownership. No existing workload is automatically killed, paused,
reniced or retuned. Conditions may change after admission. When available, an increase in the existing psutil swap-out byte counter across
the fresh window also vetoes new auxiliary admission. Historical nonzero swap
usage alone is not a veto. Missing swap counters do not replace the mandatory
CPU/available-RAM checks; a separate memory-pressure tool/subsystem is deferred.

Write scopes default to resolved working directories. Same, parent/child,
missing or inaccessible trees prevent coexistence. Independent scopes do not
prove safe scientific dependencies: `--independent` also asserts that the
auxiliary neither needs the primary to finish nor consumes unsafely changing
outputs. There is no detailed file-access analysis or scientific artifact handoff.

### External VASP plus a prepared independent analysis

First inspect the actual observation; do not guess an E ID or derive budgets from
NPROC/current usage. This non-executed template assumes the user has independently
verified the external launch bounds and prepared an analysis command with matching
thread/memory behavior:

```sh
cmw jobs status
cmw jobs show E_REPLACE_WITH_CURRENT_OBSERVATION_ID
cmw jobs config --mode bounded-sharing --cpu-budget 10 --memory-budget-gib 24 \
  --cpu-reserve 1 --min-available-gib 4
cmw jobs reserve E_REPLACE_WITH_CURRENT_OBSERVATION_ID --cpus 8 --memory-gib 8 \
  --write-scope /absolute/vasp-case
cmw jobs add --name independent-Multiwfn --engine Multiwfn \
  --cwd /absolute/completed-independent-analysis --role auxiliary \
  --cpus 1 --memory-gib 1 --independent --trust-resources \
  --write-scope /absolute/completed-independent-analysis \
  -- /bin/bash /absolute/completed-independent-analysis/prepared-run.sh
cmw jobs status --json
cmw jobs start
cmw jobs watch
```

The example budgets are user declarations, not Multiwfn defaults or enforced
limits; Multiwfn is not inherently lightweight. Prepare and verify its own
threading/settings before using the trusted contract. The analysis directory
must be independent of the ongoing VASP case. No real Multiwfn or VASP is launched
by documentation validation.

`reserve` records one identity-bound coexistence declaration from a fresh complete
external observation. It keeps the workload external/read-only and the guard
BUSY. It never creates a managed attempt or signalling authority. The binding
includes current verified family members and executable/launcher/group evidence;
changed identity, membership or disappearance invalidates it conservatively.
Unknown metadata or an additional unreserved external computation blocks new
admission. An inaccessible/inconsistent external cwd needs an explicit write
scope. `cmw jobs unreserve` revokes consent without touching running processes.
A lingering auxiliary still blocks the next primary after its external anchor
expires. No new VASP can inherit an earlier reservation through PID reuse.

The console shows commitments, roles and the auxiliary slot separately from live
CPU/RSS. In narrow tables `P`/`A` beside the ID means Primary/Auxiliary. Press B
for the complete read-only scheduling decision: anchor, external reservation,
write scopes, headroom, policy, warnings and exact refusal reason. Enter retains
job/external details; external actions remain read-only. JSON exposes the same
`scheduler`, `admission`, `scheduling` and `sharing_anchor` data without formatted
numeric values. Sampling alone writes no telemetry history.

### Isolated synthetic demonstration

This optional demonstration creates a separate queue and two benign waiting shell
commands. It does not use scientific engines. Observe admission with `status`;
headroom may legitimately prevent overlap on a busy host.

```sh
demo=$(mktemp -d)
mkdir "$demo/primary" "$demo/auxiliary"
cmw jobs --state "$demo/state" config --mode bounded-sharing --cpu-budget 2 \
  --memory-budget-gib 1 --cpu-reserve 1 --min-available-gib 1
cmw jobs --state "$demo/state" add --name primary-fixture --cwd "$demo/primary" \
  --cpus 1 --memory-gib 0.25 --allow-auxiliary \
  -- /bin/sh -c 'while [ ! -f release ]; do /bin/sleep 0.1; done'
cmw jobs --state "$demo/state" add --name auxiliary-fixture --cwd "$demo/auxiliary" \
  --role auxiliary --cpus 1 --memory-gib 0.25 --independent --trust-resources \
  -- /bin/sh -c 'while [ ! -f release ]; do /bin/sleep 0.1; done'
cmw jobs --state "$demo/state" start
cmw jobs --state "$demo/state" status
# After both are Run, release only these fixture commands:
touch "$demo/primary/release" "$demo/auxiliary/release"
# Confirm both Done before stopping this isolated controller:
cmw jobs --state "$demo/state" status
cmw jobs --state "$demo/state" stop
```

Do not treat a temporary state directory as isolation from real external engine
observation: an unreserved real VASP correctly blocks this demonstration. The test
suite uses a private fixture-observation seam to avoid dependence on research
processes; there is no production bypass. The feature does not implement arbitrary
N-job scheduling, HPC, automatic retry, global machine locking, process adoption,
SIGSTOP/SIGCONT time slicing or hard resource enforcement.
