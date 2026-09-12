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

### EXEC-002 — fixed

The old function skipped a leaderless subgroup because it queried the missing
leader PID; the focused regression reproduced the missing TERM target. Signal
authority now comes from current member birth identities and rechecked SID/PGID
membership under the verified pinned session owner. Subgroups are still signalled
before the pinned leader. A vanished witness can be replaced by another verified
member; an existing group without one fails closed. Numeric PGID alone never
authorizes a signal. POSIX topology checks and signal delivery are not atomic;
the existing portable check-to-syscall race remains a limitation.

Validation: 19 focused ownership, completion and cancellation tests passed.
Real fixtures reap the subgroup leader before readiness, verify the survivor's
birth/SID/PGID, then exercise cooperative TERM and resistant KILL (exit 137,
signal 9). A resistant survivor keeps its CPU commitment until confirmed ended.
An unrelated session remains alive and un-signalled. Primary/Auxiliary isolation
passes in both default group mode and opted-in session mode. Scoped independent
review found no signal-authority weakening; final fixture cleanup handles a
verified terminal transition racing cancellation.

### SEC-001 — fixed

The original enqueue path accepted an existing mode-0755 state root. All Store
mutations and both start/controller entrypoints now require a POSIX root owned by
the effective user with no group/other mode bits. New roots use 0700; unsafe
existing roots are refused before creating locks or sensitive metadata, with the
path, mode, owner and private-directory guidance. No existing directory is chmodded.
Read-only snapshots/status preserve existing permissions and do not create state.

New databases, journals, locks, receipts and logs are 0600. The shell restricts
log creation in a subshell, preserving the scientific payload's inherited umask.
Existing files remain protected by the private root without changing their modes.
The policy checks POSIX ownership/mode; it is not encryption.

Validation: 20 privacy/store/display/diagnostic tests and 18 guard/telemetry/session
runtime tests passed, followed by all seven final privacy cases and shell syntax
validation. Umask 0000 and 0077 both produce private state files while payload
output modes remain as requested. Schema-1 read/migration, active legacy refusal,
future-schema rejection and current state behavior remain intact. Independent
review identified and closed the direct-controller lock-before-validation path.

### EXEC-003 — fixed

The standalone regression reproduced alias deletion while a TERM-resistant direct
child remained alive. The shell now tracks its exact child in its own running and
stopped job entries, observes until completion or interruption, and only reaps
after the child has ended. TERM/INT starts a bounded cooperative wait, then KILL
and bounded drainage. Confirmed shutdown retains exit 143/130 respectively.
Unresolved termination returns 75 with an explicit diagnostic and retains aliases;
EXIT cleanup cannot delete resources still needed by that child. No name-based
or unrelated-group signalling is introduced. Scientific settings are unchanged.

Validation: all 32 affected fake-Multiwfn runtime, IFCT and cube workflow tests
passed, including five interruption cases. These cover the pre-wait signal race,
TERM/INT resistance, Jobs wrapping and failed KILL with live-child alias retention.
Cooperative behavior, ordinary exit status, stdin, threads and settings remain
covered. Unrelated sentinel processes survive with the same birth identity.
Shell syntax and independent lifecycle review passed; the timing fixture's final
bounded startup refinement was checked separately.

### SEC-002 — fixed in current publication inputs

The new bounded screen reproduced seven files with personal home paths. Review
also identified four per-user macOS temporary paths in newer parser logs. In
total, 40 path prefixes across nine reduced logs and two provenance manifests
now use logical `/fixture-root` aliases. Field names, basenames, relative lineage,
versions, original source/settings hashes and scientific values are preserved.
Manifest notes distinguish original extraction from publication transformation;
changed reduced-file hashes/sizes identify the new bytes, while their previous
reduced identities remain recorded. No scientific source or provenance was removed.

Validation: 43 affected parser/provenance/hygiene tests and all five final hygiene
cases passed. Byte comparison after replacing only the declared roots proves
every other log byte is unchanged, including carriage-return records. The screen
checks all tracked content and wheel/sdist members without archive extraction;
only the nine known parser log names are exempt from the runtime-log filename
rule, and their contents are still scanned. Oversized inputs fail inspection.
This is a bounded publication check, not a universal secrets or licensing audit.
Final built-artifact checks follow in DIST-001. Earlier Git history and the
immutable external audit package retain their original evidence and are unchanged.

### DOC-001 — fixed

README, top-level Jobs help, the telemetry module contract and the detailed
resource explanation now agree: Sequential is default; explicit Bounded Sharing
permits one Primary plus at most one Auxiliary. Observations can veto admission
but cannot grant ownership, certify completion or release low-usage commitments.
Requested, committed, observed and enforced resources remain separate; there
are no hard OS limits. Explicit coexistence consent does not relabel external
BUSY as idle. Historical development records are preserved.

Validation: focused text/diff review and generated top-level/Jobs help. The README
test installation includes the optional console dependency so its documented
broad command also covers that interface. No execution behavior was changed.

### Remaining campaign

DIST-001 and the final broad/artifact gates are pending. PERF-001 and the
architecture/scaling expansions listed in the request are explicitly deferred.
Intentional ownership, admission, resource, scientific
validity and fail-closed `Unknown` distinctions remain required throughout.
