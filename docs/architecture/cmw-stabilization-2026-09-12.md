# CMW stabilization — 2026-09-12

This work record follows the external forensic audit of `8edee08` and tracks
issue-scoped remediation. It is not a release announcement. Version and state
schema remain unchanged. All execution evidence uses isolated state, synthetic
engines and owned benign processes; production queues and research are excluded.

## Starting evidence

The current source matched the audited snapshot: `main`, `8edee088`, 104 commits,
three unstaged display-ID feature files and its untracked test. No staged work
or intervening source changes were found. The user authorized preserving the
feature in a separate prerequisite commit, followed by its test correction.

The isolated broad baseline ran 660 tests: 659 passed, one failed, no errors or
skips (473.216 s). Only TEST-001 failed. Cancellation passed this time; that
does not explain the earlier intermittent failure. Baseline failure state and
logs are retained in the campaign's external evidence directory. A prior
restricted-sandbox run was invalid because OS process enumeration was denied;
the completed baseline used process visibility with the same isolated inputs.

## Dispositions

### EXEC-001 — fixed

The original stale-replacement race was reproduced: two contenders both
returned ownership, with the later contender overwriting the first successor.
A persistent, per-lock sidecar inode now serializes acquisition and token-checked
release using POSIX `flock`. Fresh inspection, stale removal, exclusive creation
and the completed ownership write share that guard. The guard is never deleted;
the existing ownership metadata and ordinary exclusive-create step are preserved.
Remote/unreadable ownership still fails closed. This is local POSIX coordination,
not a cluster locking protocol; mixed old/new mutating clients are unsupported.

Validation: 65 lock, cleanup and fake-ORCA tests passed. Deterministic interleavings
cover 20 competing stale recoveries, delayed release versus replacement, ordinary
contention, stale token rejection, and uncertain ownership. Both cleanup and the
ORCA wrapper exercise the shared primitive. Independent scoped review found no
material issue.

### TEST-001 — fixed

The baseline failure compared snapshots taken at different wall times. Both
snapshots now use the same injected observation time, retaining equality of every
field and rejection of all seven invalid labels. Production behavior is unchanged.
The pre-existing feature was preserved in its separately authorized prerequisite
commit, with two focused behavior tests and a fixed-time invalid-label probe.
All three display-ID tests pass after the correction.

### TEST-002 — diagnosed and fixed (runtime completion defect)

Two current failures were captured before teardown. TERM and KILL were delivered,
then the kernel group-absence probe returned transient `EPERM` immediately after
KILL. The worker abandoned its existing drainage wait and persisted `Unknown`.
Later probes confirmed absence. This is independent of EXEC-002: the original
test uses the default single-PGID contract. The unretained original audit failure
cannot be independently attributed beyond its matching regression symptom.

Both post-KILL and normal RELEASE completion now use a bounded absence waiter.
Only permission errors from the absence observation are retried, within the
existing five-second budget. Definitive kernel absence and, when enabled, empty
session membership are still required. Persistent uncertainty, live members,
and other errors fail closed; no signalling authority or commitment is relaxed.

The test launcher retains bounded logs/traces, receipts, a consistent SQLite
backup, claims, locks and identity-verified membership before teardown. Evidence
failures do not change signal forwarding or mask the original assertion. Cleanup
waits on actual owned controller subprocess handles, not arbitrary PID records.

Validation: 25/25 original lifecycle repetitions passed, all exercising TERM and
KILL, with no live recorded controller/worker/group owner after cleanup. Four
workers encountered `EPERM` and recovered to confirmed completion. Deterministic
tests cover transient/persistent permission errors, kernel presence despite empty
enumeration, session drainage, and diagnostic failure/identity boundaries.
All 30 affected lifecycle, admission-boundary and diagnostic tests passed after
the owned-handle teardown correction. Independent scoped review found no material
completion or ownership weakening.

### Remaining campaign

EXEC-002, SEC-001, EXEC-003, SEC-002, DOC-001 and DIST-001 are
pending. PERF-001 and the architecture/scaling expansions listed in the request
are explicitly deferred. Intentional ownership, admission, resource, scientific
validity and fail-closed `Unknown` distinctions remain required throughout.
