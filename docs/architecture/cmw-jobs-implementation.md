# CMW Jobs implementation record

## A. Starting checkout and compatibility boundary

- Local CMW checkout, branch `main`, HEAD `dcf09ba2f4dfdacf5e4e17f818c20ff98c86c1da`.
- No tracked or staged modifications. Pre-existing untracked ` 2` copies in source,
  tests, documentation and `uv 2.lock` were treated as user work, not feature
  targets. See the final checkout-drift note below.
- Public entry point: `cmw.cli:main`, argparse. Python >=3.10, Hatchling,
  ASE/PyYAML, unittest offline tests. CI currently publishes only the static site.
- Existing `core.execution_contract.ExecutionIntent`, execution profiles and
  v1/v2 layouts express scientific plans and prepared attempts. ORCA shell runner
  owns preflight, launch and finalization; HOF materialization creates guarded
  shell commands. Jobs invokes those prepared foreground commands unchanged.
- `JobTarget` and `ExecutionAttempt` require scientific geometry/input hashes;
  they cannot truthfully represent arbitrary trusted commands. Jobs adds only
  operational queue/attempt identity and can reference a recorded execution layout.
  It does not fabricate scientific identity or replace existing attempt metadata.
- Reuse `core.provenance.atomic_write_json` for completion receipts and
  `execution_layout.resolve_recorded_layout` for explicit prepared-layout links.
- Existing PID-only locks/process-health are advisory and insufficient for queue
  ownership; Jobs needs kernel locks, process birth identity and a pinned process
  group leader. Existing lock APIs remain unchanged.
- No general persistent local controller, transactional queue, or terminal stack
  exists. Add one SQLite store, local controller/guardian, packaged shell payload
  runner, argparse subcommands and optional Textual console. No DAG/adapters tree.
- Scientific settings, source/output directories, validation, reuse, HOF queues,
  serialized scientific states and all existing public enums remain unchanged.

### Baseline

49 focused offline tests passed (7.356 s): execution profiles/layout, job and
provenance, locks, execution intent, ORCA renderer and shell runner. Shell tests
use their own fake executables, not installed engines. No baseline failures.

## Checkpoints and completion evidence

A complete. B: transactional queue and command integration. C: detached lifecycle,
strict sequential ownership, cancellation and conservative reconciliation.
D: CLI and interactive console. E: synthetic acceptance, broad offline regression,
package checks, render evidence, documentation and focused review.

Completion requires actual sequential subprocesses, closed-terminal survival,
recovery/crash and cancellation tests, real terminal interaction, fixed-data render,
optional-dependency isolation and existing offline compatibility tests. No real
scientific engines, production state, research directories or publication actions.

## B–D. Integrated implementation

- `cmw.jobs.store`: one SQLite source of truth; immediate transactions for order,
  admission and controls; explicit pending blockers and pause/continue policy.
- `cmw.jobs.runtime`, `ownership`, `payload.sh`: controller and per-attempt
  supervisor, POSIX detached sessions, kernel singleton/worker locks, boot/host/
  birth identity, persisted intent before spawn and group metadata before GO.
  Shell retains payload launch/descriptors/traps/wait; Python owns admission,
  ownership and completion decisions. A pinned shell keeps group cancellation
  safe. Final kernel group-absence verification prevents an enumeration race
  from releasing the slot. This bootstrap/supervisor is not a scientific runner.
- `cmw.jobs.cli`: additive argparse registration; existing commands unchanged;
  explicit argv/cwd/env plus optional resolved scientific layout reference.
- `cmw.jobs.tui`: optional Textual client with stable keyed rows, responsive
  columns, selected details, bounded cached logs, events and revalidated controls.
  Rich renders literal Text in the console; ordinary CLI remains dependency-light.
- Added psutil to core requirements and a Textual `jobs` extra; updated uv lock
  without upgrading existing locked packages. No version change.

## E. Validation and review evidence

Platform: macOS Apple Silicon, Python 3.14.3, Textual 6.12.0, psutil 7.2.2.

| Evidence | Result |
| --- | --- |
| Initial affected-contract baseline | 49 passed, 7.356 s |
| Broad deterministic offline regression | 526 passed, 262.446 s; existing ASE/NumPy deprecation warning only |
| Final queue/lifecycle suite after kernel gate | 12 passed, 25.888 s |
| Added enumeration-race regression | 1 passed, 1.055 s; real missed child causes Unknown, never early dispatch |
| Updated console + actual terminal tests | 5 passed, 12.264 s |
| Concurrent selection confirmation regression | Passed; stale confirmation is rejected and the changed job is preserved |
| Real worker crash injection | Before spawn, after spawn, before GO, before completion commit all fail closed |
| Existing runner through Jobs | Existing opt+sp shell workflow with fake ORCA succeeds; validation/finalization retained; CHECK stays unevaluated |
| Package | Hatchling wheel built; packaged shell present; pre-existing duplicate files not included |
| Editable CLI | Installed and `cmw jobs --help` works |
| Core-only installed wheel | Textual and Rich absent; public imports/existing CLI work; watch gives actionable error; synthetic queue and cleanup pass |
| Lock and shell checks | `uv lock --check --offline`, `bash -n` and focused whitespace/diff review pass |

The 526-test run preceded the additional crash-boundary and enumeration-race
regressions and final console polish. Those changes received the focused checks
above; unchanged existing suites were not rerun for reassurance. Queue tests now
cover A/B/C non-overlap, persisted reorder/hold, pause without stopping the active
job, failure pause/continue, singleton and concurrent clients, stdout/stderr,
Unicode paths/environment, missing executables and execution failure, an outer
wrapper exiting before its child, cancellation including TERM-resistant children,
unrelated-process survival, restart without duplication, stale PID identity,
spawn/completion crashes and kernel-level completion gating.

The PTY test uses actual `App.run`, not the headless widget driver. It closes the
interactive shell that started the controller while A is active, then exercises
watch details/logs, Q, Ctrl-C and PTY closure. A is gated until all detach modes
have been exercised; B/C dispatch only after A is released and fully ends. Test
controllers and synthetic processes are stopped, with no production queue used.
The widget tests exercise hold/release/order/cancel confirmation, selection,
scrolling, resize and log-tail caching. Fixed-data/time wide (130×32) and narrow
(64×28) SVG render evidence is generated by `test_fixed_render_evidence` when
`CMW_JOBS_FIXED_RENDER` is set to an output prefix. Both final layouts were
rendered and visually inspected. Images use only synthetic example paths.

### Compatibility matrix

| Existing surface | Assessment |
| --- | --- |
| Public identities/imports, CLI argument contracts, profiles, ExecutionIntent/preflight, renderer, layout v1/v2, state readers, locks, provenance/reuse/finalization | Verified by baseline and offline suite |
| HOF adapter/planner/batch/full-chain, ORCA shell runtime, opt/freq/SP, Multiwfn/IGMH/cubes, stacking and structure conversion | Verified with deterministic local fixtures/fake engines by offline suite |
| Installed core without console dependencies | Verified by separate clean temporary wheel environment |
| Pre-existing failing tests | None observed; ASE/NumPy warning is not a failure |
| Real ORCA/VASP/Multiwfn/MPI calculations; Linux/Windows; logout/reboot survival | Not exercised; real calculations forbidden by task, Linux unvalidated, Windows/reboot survival unsupported |

### Environment limitations and checkout drift

The restricted sandbox denied macOS boot identity/sysctl and some PTY/preview
operations. Authorized synthetic tests ran with that local access rather than
weakening ownership checks. Build isolation required package-index access.
The local editable installer marked its new `.pth` file hidden; Python 3.14
skips hidden `.pth` files. Clearing the hidden flag on that task-installed file
restored the CLI. No existing package source or unrelated environment was altered
as a workaround.

The pre-existing untracked ` 2` copies seen at initial inspection were absent at
final inspection (including documentation and `uv 2.lock`). No task cleanup
command targeted these files; their disappearance is unexplained checkout drift,
not a claimed preservation or task-authored deletion. No tracked changes outside
the feature were observed. The implementation did not modify research repositories
or launch real scientific engines. No commit, push, tag, release or service
installation was performed.

Known limitations are documented in `docs/cmw-jobs.md`: local foreground groups,
no arbitrary daemonization/escape, no automatic retry/checkpoint/reboot survival,
no invented scientific checks, no snapshots/immutability of scientific files,
and no unsafe override when Unknown ownership cannot be resolved.

Final scope: 4 tracked files modified and 13 feature files added; HEAD remains
`dcf09ba`. No known introduced major defect remains after the checks above.
A final read-only process check found no temporary Jobs controller/worker left
running. No other research checkout was modified.

## Bounded pre-commit disposition review

Reviewed the actual uncommitted feature on `main` at `dcf09ba`; nothing staged.
The operational store/identity and controller/supervisor distinctions are justified;
no consolidation, public API reduction, dependency replacement or cleanup was
warranted. Existing scientific runner, intent, profile, provenance and layout
contracts remain unchanged. No private paths/data, debug artifacts or unresolved
in-scope TODO/FIXME were found in the proposed feature files.

Two concrete environment defects were reproduced and corrected:

- `payload.sh` relied on payload PATH to find internal `env`/`sleep`. An empty
  PATH let an absolute payload finish but prevented its exit receipt. Internal
  utilities now use their absolute platform paths.
- `runtime.launch_blocker` resolved relative/empty PATH entries against the
  detached controller directory, incorrectly blocking executables in job cwd.
  Its lookup now matches the payload working directory without rewriting PATH.

Added a synthetic regression covering relative, empty and absolute PATH lookup
and receipt completion. Replaced short execution windows with explicit release
files in the pause, active-controller-restart and missed-child tests; their
assertions and ownership checks remain intact. No structural refactor occurred.

New validation: both pre-fix defects reproduced; 4 targeted tests passed in
8.169 s, then all 20 Jobs tests passed in 47.781 s (including fake-engine runner
integration and actual terminal interactions). Shell syntax, focused diff and
private-data/artifact scans passed. Source-path parser initialization imports
neither Textual nor Rich. Dependency-lock inspection found no existing version
changes. The prior 526-test offline result is reused, not claimed as a new run.

The local editable `.pth` is again marked hidden, and plain environment imports
failed during this review. Source-path execution passes; prior separate core-wheel
installation evidence is unchanged. This local environment issue is not a source
commit blocker and was not worked around by modifying package code or the environment.
Disposition after the narrow fixes: ready to commit. No commit or publication
operation was performed.

## External Activity Guard refinement — plan and compatibility boundary

Starting point: clean `main` at `3ba21395f66308de93235fe6df6d4f4bfac416d5`.
The default macOS user state resolves to `~/Library/Application Support/cmw/jobs`;
no state override was present in the viewing command. Its controller was offline
and it contained no managed attempts. Read-only process metadata on this Mac
showed eight live `vasp_std` executables under a common birth-identified `prte`
launcher/session. The old projection only displayed SQLite jobs; it had no
external observer. These processes are external to this selected queue. Another
queue origin was not established. No scientific files or process environments
were read and no production process/controller was changed.

Plan: add one shared bounded process observer with explicit evidence/coverage,
reuse Jobs birth identity and verified group ownership, extend status JSON
additively, and add a separate read-only console panel. Evaluate admission before
claim and again in the supervisor before payload GO, preserving pending order
and user dispatch intent when blocked. Validate through fixture-owned process
metadata and existing synthetic lifecycle tests, then a fresh installed wheel.

The guard is best-effort avoidance of recognized accessible current-user local
executables, not a global machine lock. It cannot exclude manual launches after
its scan or coordinate independently configured queues atomically. Observation
never grants cancellation/ownership or establishes scientific completion. Existing
SQLite managed identities, queue lifecycle, runner, receipt and scientific
contracts remain authoritative. No version/dependency changes or publication.

### Guard implementation and acceptance evidence

One new `jobs.activity` module shares bounded collection, classification and
admission projection across controller, supervisor, CLI and TUI. Existing Jobs
identity/lock helpers, queue transactions, process-group lifecycle and packaged
payload are retained. psutil 7.2.2 source inspection confirmed that process_iter,
creation time and executable metadata cache values, and Process.exe may fall
back to argv[0]. The observer therefore uses fresh identities and the platform
executable accessor, without clearing a shared cache or reading command lines.

Independent boundary review found and corrected three coupled issues: strict
lock-access evidence is required before excluding managed candidates; a pending
attempt's still-held supervisor lock prevents immediate reclaim; and refused
admission preserves concurrent Unknown or changed-claim state. Final launcher
revalidation clears obsolete grouping while retaining every surviving worker.

Newly executed checks: all 51 Jobs tests passed (75.905 s), including 15 observer
fixtures, six lock/rollback boundary regressions, five real guard integration
cases, existing lifecycle/runner/PTY checks, and external CLI/TUI tests. Another
60 existing profile/layout/intent/lock/provenance compatibility tests passed
(0.550 s). These are current runs; the older broad offline result above is not
claimed as rerun. Integration discovery is restricted through a test-only launcher
to birth-verified fixture PIDs; no public production bypass exists. Two acceptance
tests were further changed to wait for explicit guard transitions instead of
fixed timing windows, with a focused follow-up run recorded separately.

Read-only live acceptance identified the existing VASP family through its OS
executable path, eight process identities and a common dedicated MPI launcher
session while the selected queue was empty/offline. Same-user executable-access
blind spots were also reported, not suppressed to force NO_MATCH. Group count
was not claimed as allocated CPUs, and no scientific completion was inferred.

Test-environment limitation: two early attempts to execute benign copied system
sleep binaries became orphaned in macOS uninterruptible exec state. Exact-owned
identity cleanup with SIGCONT/SIGKILL did not clear them during bounded checks.
Subsequent real tests use ordinary Python release-gated fixtures and those exit
and are reaped normally. The two kernel-stuck copies prevent claiming that no
synthetic processes remain. No research process was signalled; no OS service,
production controller or existing editable environment was modified to resolve
this environment problem. This limitation must remain explicit in closeout.

Follow-up evidence: the two explicitly synchronized guard tests passed (5.689 s).
A real metadata test additionally proved selected-queue managed exclusion while
an independent process with the identical executable still blocked (3.633 s).
A final projection regression ensures existing executable preflight also remains
visible as blocked admission; the directly affected CLI/TUI checks passed with it.

The final source wheel was built again after that projection correction and
installed outside the checkout into a second fresh environment using unchanged,
platform-aware locked dependencies. Core-only import resolved to site-packages;
all Jobs package bytes matched source, including activity.py and payload.sh;
status/JSON worked without Textual/Rich and watch reported its missing extra.
After installing the supported jobs extra, the actual terminal console rendered
external VASP and detached normally. Its isolated detached controller observed
the live blocker and kept the synthetic job pending without claim/start/marker,
then stopped. The ordinary default user state remained empty and offline. The
existing editable import still failed and was not repaired.

The initial temporary installer stalled on downloads; an attempted hand-written
constraint list also exposed duplicate platform versions. Neither was a source
defect. Exporting the unchanged lock with its proper platform markers resolved
the temporary installation. No dependency lock or package metadata was changed.

Final live observation still showed eight VASP processes, with executable and
verified launcher/session evidence, external/unattributed to the selected queue.
Five pre-existing same-user metadata blind spots remained, plus one from the
kernel-stuck test fixture. They remain coverage warnings and would prevent a
clear admission claim even after the VASP observation disappears. Both abandoned
copied fixtures were still STAT UE at final inspection; no further unsafe cleanup
or system intervention was attempted. All later fixtures and wheel-smoke
controllers exited normally. No commit, push, tag, release or version bump.

## Bounded final caveat investigation

Starting state remained `main` at `3ba21395f66308de93235fe6df6d4f4bfac416d5`,
with the guard changes unstaged/untracked and no staged changes. No existing
work was discarded. The applicable working agreement requires read-only research
observation and does not require another signal to the known test copies.

PIDs 11338 and 11897 still matched births 1788932262.479477 and
1788932319.953141 on the same host/boot. Both were parented by PID 1, had no
observed descendants, and remained `UE`; the installed ps manual defines this
as uninterruptible wait plus trying to exit. Wait channel was `-`, so no exact
kernel wait reason was established. Their PGID/SID pairs were 11323/11323 and
11888/11888. The first OS executable path was unavailable; the second still
identified the temporary copied binary. Both retained copied-sleep argv and
checkout cwd. No additional signals were sent.

The creating agent's original tool evidence traces the first to the abandoned
external_fixture in the pause test: copyfile('/bin/sleep', temporary binary),
chmod(0700), then direct Popen([binary, '60']). The second came from a standalone
diagnostic copyfile/chmod/Popen([binary, '10']) snippet. Neither used CMW's
controller, supervisor, payload helper or GO path, nor explicit new group/session
settings. The first terminate/wait timed out; the second diagnostic's intended
kill/wait does not establish exactly where it stalled. No code-signing or kernel
root cause was proven. Classification: TEST-ONLY / MACOS EDGE CONDITION — NOT A
SOURCE BLOCKER. Ordinary CMW can launch arbitrary prepared executables, but no
evidence implicates its production process-management logic in these artifacts.

A narrow guard policy defect was demonstrated: absent executable paths on
unrecognized ordinary same-user process names were treated as material blockers.
Those are now explicit non-blocking scope limitations, not proven engine matches.
A recognized default or configured executable basename with missing critical
metadata still fails closed; unknown user/name scope and scan failures still
block. This supersedes the previous blanket executable-gap policy above. No
per-PID ignore mechanism, production configuration change or global bypass was
added. Four directly relevant tests passed (0.009 s), including the new regression
for ordinary missing paths and configured-name uncertainty.

Actual normal-state observation remained BUSY for one eight-process VASP family.
A labelled read-only counterfactual, excluding only that family's freshly matched
PID/birth identities, returned NO_MATCH with zero material uncertainties. Five
ordinary-process executable gaps and the first UE fixture remained non-blocking
coverage limitations; the second UE copy was unrecognized by its actual path.
The normal queue remained empty, offline and dispatch-disabled.

The real console was launched through the verified source CLI against the explicit
normal state, in an interactive PTY. Its live terminal output showed VASP, NPROC 8,
representative PID 90828, external/read-only ownership, BUSY and blocked admission.
The user subsequently supplied an actual macOS Terminal screenshot confirming
the visible external VASP row, NPROC 8, representative PID 90828, read-only label,
BUSY/blocked guard, offline controller and dispatch OFF. Visible console
verification is complete. The console was left open; no controller was started.


## Runtime CPU/RAM telemetry refinement

Current resource usage is an ephemeral client projection layered onto the existing
activity/ownership evidence. `cmw.jobs.telemetry.Sampler` retains per-identity CPU
baselines for one viewing client. The shared activity projection supplies current
membership; telemetry does not decide ownership or admit execution. It adds
`machine_usage` and workload `usage` objects without changing requested-resource,
managed-state or external-guard fields. No database history or metric events are
introduced.

Machine CPU uses host CPU-counter deltas as a percentage. Machine RAM is total
minus available, with total separately retained. Workload CPU is summed verified
member CPU-time deltas divided by monotonic elapsed time, in logical CPU
core-equivalents. Workload RAM is summed current RSS, with the explicit shared-page
double-count caveat. CPU requires a baseline; newly observed members yield
warming-up/partial quality instead of fabricated zero. Quality, member coverage,
measurement age and stale flags accompany numeric values; telemetry failure never
changes BUSY/NO_MATCH or Run/Unknown.

The existing two-second TUI background observation cycle owns a persistent sampler.
Rendering only ages cached measurements, with the shared five-second freshness
threshold. Managed cache entries are keyed by attempt identity and attached only
while the latest managed snapshot remains active. Both managed and external
read-only detail panels refresh from that cache. Wide and narrow tables display
CPU NOW and RAM NOW, while declarations retain requested resources in details.
CLI status/show use the same projection; a fresh one-shot CPU sample is normally
warming up. The machine header remains distinct from workload usage.

Validation targets are the existing local macOS/psutil runtime, deterministic
format/model and console fixtures, owned synthetic processes, and final
current-wheel/live observation acceptance. No historical
peaks, resource enforcement, scheduler changes or new scientific claims are part
of this refinement.

Current refinement validation (9 September 2026): 81 Jobs tests passed, followed
by the focused console test after the final active-attempt cache pruning. The
suite includes deterministic CPU/memory and identity cases, owned Python
CPU/allocation/idle fixtures, separate external workloads, surviving managed
payload children, and unchanged guard behavior under metric denial. Missing
pinned group-leader behavior is tested deterministically; the real managed
fixture's packaged shell guardian remains until its release handshake.

One fresh wheel was built and installed outside the checkout into a new Python
3.14 environment using the locked dependency constraints. Core import, packaged
payload shell, status/JSON, missing-extra behavior and subsequent Jobs-extra
console initialization passed. The installed console rendered a gated synthetic
workload at about 0.97c with 36,339,712 RSS bytes and fresh machine metrics. Its
owned process exited through the release gate and was reaped; viewing did not
create a queue database.

Read-only normal-state observation still found the VASP family represented by
PID 90828 with eight members. The newly launched terminal session rendered 8.0c,
1.1 GiB RSS, machine CPU 77%, machine RAM 27.7/36.0 GiB, and BUSY/blocked admission
with controller offline and dispatch OFF. These are interval runtime readings,
not scientific results. Actual desktop-window visual acceptance remains pending:
the app queued opening the new console panel, while its terminal-reader tool
returned a separate shell prompt. That limitation must not be described as a
failure to detect VASP or as completed visible-window acceptance. No research
process was modified, production controller restarted, or real calculation
launched. This refinement has not been committed or pushed.

The user subsequently supplied the 14:49:09 actual Terminal screenshot, resolving
the visible-window gate: VASP PID 90828 was visibly read-only with 7.1c, 0.9 GiB
RSS, machine CPU 96%, RAM 27.7/36.0 GiB and BUSY/blocked admission. At the user's
request, workload CPU presentation now multiplies core-equivalents by 100 and
displays an integer percentage (7.1 cores becomes 710%). Details explain that
100% means one logical CPU; machine CPU keeps its whole-host scale. The sampler
and raw JSON `cpu_cores` field are unchanged. This is a display-only refinement.

## Bounded Sharing implementation plan (11 September 2026)

Starting checkout: `2262be57b0aee82dc1b024668c1670f5a125f34a`, schema 1.
Pre-existing uncommitted work adds dedicated-session ownership in activity,
ownership and runtime, related documentation/tests, and a prepared Multiwfn IFCT
runner. Preserve those changes and their scientific/execution contracts.

The store already represents multiple jobs and supervisors; controller tick
reconciles every active attempt, and cancellation/finish target one attempt.
Single-slot assumptions to replace are tick's unconditional active veto, the
final GO guard-only permission, snapshot's first-active waiting reason, and
hard-coded Sequential presentation. Telemetry already maps all active attempts.

Implement a shared admission evaluator and bounded observation window. Keep
Sequential as the migration/default policy. In explicitly configured Bounded
Sharing, permit only one consented primary plus one explicitly independent,
trusted-resource auxiliary, selected in stable auxiliary order. Persist role,
consent, write scope and the admitted anchor on jobs; persist one identity-bound
external scheduling reservation without changing external ownership/BUSY.

Starting atomically consumes declared CPU/RAM inside the existing transaction.
Both tick and final GO re-evaluate persistent order/roles/anchor, commitments,
external evidence, disjoint resolved write scopes, and fresh sustained host
headroom. Running work is never automatically stopped for pressure/overrun.
A lingering auxiliary blocks the next primary. Use independent existing
supervisors and preserve failure/pause/cancellation semantics.

Validate schema-1 migration, policy thresholds/order, synthetic managed and
external overlap, transactional and final-GO races, restart/cancellation, and
CLI/TUI projections. Then run the relevant Jobs suite and one broad offline
compatibility regression, followed by one final-source installed-wheel smoke.
Only isolated synthetic jobs/configuration are authorized for acceptance.

### Implemented disposition

`sharing.py` supplies the single admission decision used by controller claims,
the supervisor's final GO transaction, CLI/JSON and the console. Schema 2 adds
explicit scheduler policy, per-job role/consent/independence/resource contract and
resolved write scope, plus one external identity-bound reservation. Schema-1
read-only views supply compatible defaults without writing; migration is atomic
and refuses active/unresolved legacy attempts or a live/locked legacy controller.
This prevents an older supervisor from encountering an unsupported schema during
finalization. No production state was migrated for testing.

Sequential keeps the ordinary single-slot/global-order policy. Bounded Sharing
selects only the first pending auxiliary during an active sharing window, with a
held auxiliary blocking that lane. Without an anchor, the ordinary global queue
order applies. Starting is the transactional resource commitment; Run,
Cancelling and Unknown retain it. Missing declarations remain unknown in the
display rather than becoming zero. Final GO rechecks the same decision with
independent host evidence, current controls/order and the admitted anchor. A
refused auxiliary returns at its original queue position, without payload work.
Each existing supervisor retains its own ownership, logs and cancellation scope.

CPU/RAM commitments include a valid external declaration without changing BUSY or
external ownership. Auxiliary CPU/RAM, independence and `trusted-declared` consent
are mandatory; known MPI ranks times threads/rank cannot exceed declared CPUs.
The existing Multiwfn runtime prepares explicit per-run thread settings and checks
contradictory declarations; opaque prepared commands still require the user's
explicit trust assertion, not an engine-name exemption. Existing runner inputs,
finalization and scientific contracts are unchanged.

Host admission uses two fresh observations at least 0.5 seconds apart: oldest at
most five seconds, newest at most two seconds. Worst observed free logical CPU
capacity must cover candidate CPUs plus reserve, and minimum available RAM must
cover candidate memory plus margin. Recent available swap-out counter growth is
an additional veto; historical swap usage is not. Two CPU samples more than
0.05 core above a declaration, or a current RSS subtotal above declared memory,
warn and veto new admission. Partial measured subtotals can demonstrate overruns;
shared-page RSS double counting remains explicit. Resolved equal or ancestor/
descendant write scopes block sharing. These are admission checks, not resource
enforcement, and never automatically stop running work.

Validation: the broad offline regression passed 644 tests in 322.872 seconds.
The new gated runtime cases cover managed/external overlap, single auxiliary and
primary lanes, a lingering auxiliary, pause/hold, both-job restart, independent
failure/cancellation, external expiry, simultaneous claim transactions and final
CPU/RAM/external GO refusals. Deterministic cases cover exact commitments,
headroom/freshness/swap, identity changes, write scopes and overrun warnings. A
small owned real CPU fixture produced fresh bounded sampler evidence and was
reaped without exact-percentage assertions. CLI/JSON and wide/narrow console
checks retain the 713%-style display and separate external read-only controls.
Following final refinements, 31 policy/store/UI tests passed in 1.603 seconds;
the final external-GO queue-position regression also passed. The broad suite was
not redundantly repeated after those focused refinements.

During this task a separate concurrent commit, `14fc760` (queue-managed IFCT
runner), advanced HEAD and tracked the three pre-existing IFCT files. That work
and the pre-existing session-ownership changes were preserved. This task made no
commit, push, tag or version change. Final installed-wheel results follow below.

One final-source `computational_modelling_workflow-0.1.0-py3-none-any.whl` was
built and installed in a fresh Python 3.14 environment outside the checkout under
`/tmp/cmw-sharing-wheel.htLXoR`. Imports resolved from that environment's
`site-packages`, with no checkout import path. Core-only imports, packaged payload
shell, CLI help, human/JSON status, expected missing-console-extra error (exit 2),
and isolated schema-1 read/migration passed. Installing the supported Jobs extra
from locked cached dependencies enabled the actual console.

Installed-wheel gated synthetic Primary + prepared Multiwfn-labelled Auxiliary
overlap passed, with deterministic private test headroom evidence. The next
Primary stayed queued after the first Primary ended until the Auxiliary also
ended. An actual 150-by-42 PTY rendered Bounded Sharing, both roles, commitments
and machine CPU/RAM; Q detached while both executions continued. All three jobs
ended Done/exit 0, the isolated controller stopped, and owned runtime identities
were verified gone. Eight documentation commands, including the prepared
Multiwfn example, parsed against the installed CLI without execution. Evidence
is recorded in that temporary directory's core/runtime/CLI summary JSON and PTY
log. The separate owned CPU-sampler test supplies real host integration evidence;
the package concurrency test does not claim real-host spare-capacity acceptance.

Disposition: BOUNDED SHARING IMPLEMENTED — ACCEPTANCE PASSED. No real scientific
engine ran, no research process was signalled or modified, and no production
queue, reservation or controller was mutated for acceptance. New test fixtures
were released/reaped; unrelated historical processes were not touched. Limits
remain one primary plus one auxiliary, explicit trusted resource declarations,
admission-only enforcement, conservative external identity/write-scope checks,
and no machine-wide exclusion, arbitrary backfill, automatic tuning or HPC.
