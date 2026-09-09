# CMW Jobs: local sequential execution

CMW Jobs runs already-prepared, trusted foreground commands in an explicit order.
Exactly one execution in this queue may own the execution slot. External programs
started outside Jobs are outside this guarantee. This is a local POSIX feature;
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

Hold and order apply only to jobs that have not started. A held or blocked queue
head blocks later jobs. Move it explicitly, release it, fix its launch blocker,
or cancel that pending job. The persistent ORDER is independent of job ID and
screen row order. The console keeps rows stable while ORDER changes.

Console keys: Enter details, L logs, P toggle dispatch, H hold/release, O order,
X cancellation confirmation, Q or Ctrl-C detach. Arrow/Page keys scroll. Narrow
layouts retain ORDER, ID, status, requested CPUs and elapsed duration; names,
engine, reason and resource details remain in the selected-job panel. Very small
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
| BUSY | Recognized live external computation observed; block new dispatch |
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
ordinary sequential dispatch proceeds only while intent remains enabled and all
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
global machine lock. The existing at-most-one-execution guarantee applies to the
selected managed queue only.

An already-running controller retains the code it loaded. To obtain this guard,
use the normal explicit stop/start lifecycle when you choose; installing or
opening a new client does not upgrade that controller. No production controller
was restarted as part of this refinement. An already-open watch client also needs
to be detached and reopened to load new display code.
