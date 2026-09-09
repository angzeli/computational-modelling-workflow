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
