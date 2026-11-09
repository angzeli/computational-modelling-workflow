# VASP managed foreground ownership qualification

## Current canonical runner and qualification

D31 now selects VASP port commit
`3c438192beb19ff9fc209c3fdcba3f5678a20e6e` (R), including the issue #19 repair
from port [PR #2](https://github.com/angzeli/vasp-6.6.1-macos-arm64-port/pull/2)
and [PR #5](https://github.com/angzeli/vasp-6.6.1-macos-arm64-port/pull/5).
Its `scripts/launch.py` Git blob is
`a534d39a522ebe6b9be0a6516d79f87b341fcafd`, identical to the qualified launch
file at `c4aa0aa`. The qualified pair is CMW
`f3b052207f3a827636d870d2784db2c32b9f331e` plus R.
[Port CI run 42534521961](https://github.com/angzeli/vasp-6.6.1-macos-arm64-port/actions/runs/42534521961)
passed all 43 tests and 2,226 synthetic cases on each qualified platform:
Ubuntu/Python 3.11 and macOS/Python 3.14.

The repair distinguishes VERIFIED, GONE and UNVERIFIABLE, retains the pending
operation while a probe is unreadable, holds denied sends without inventing a
termination intent, and guards the CMW ownership capability before MPI starts.
The original drainage operation resumes after recovery; ordinary MPI exit does
not become signalling cleanup merely because an observation failed.

This is synthetic pair qualification, not deployment. The Mini's real-VASP
runner smoke and [deployed-runtime parity](deployed-runtime-parity.md) against
the actual deployment candidate are deferred to the authorized cutover window
after the research job finishes. Neither was performed in this wrap-up, and no
Mini contact occurred. Future D18 work is based on R, not `51d8209`.

## Historical runner and Boolean compatibility

The VASP port owns MPI supervision in `scripts/launch.py:supervise`. The file
was identical at `f7afe0a` and the historical D31 base `51d8209` (Git blob `5a51f95`).
At lines 70–77 it imports CMW's identity, membership and signalling helpers,
pins the session leader and checks the initial topology before spawning MPI.
Lines 102–115 check that pin when enumerating surviving members. Lines 117–135
recheck before subgroup TERM and KILL. Lines 177–178 wait for drainage at MPI
exit. The exception path at 201–207 can also enter cleanup. The runner has its
own control flow and uses CMW's implementation of process identity.

Historical runners call the Boolean `owner_alive` interface. CMW accepts a
verified observation immediately and refuses a confirmed loss immediately. On
unreadable evidence it tries one immediate complete reread. Old psutil
could recover on the explicit birth read after a denied construction read.
Omitting sleeps also limits how far exception cleanup can advance into a
recovering observation window. No retry starts after a 100 ms monotonic
deadline. A kernel call already in progress can finish later. The adapter
only reads evidence. A final false result can mean either confirmed loss or exhausted retries. Callers that need
to distinguish these outcomes use `observe_owner` and own their retry policy.

## Historical-runner parity

At a stable clock, CMW must match or improve on both historical revisions. Across a wall-clock step, it must behave as the historical revisions do at a stable clock. A permanent refusal caused by the old boot timestamp cannot be the reference for legitimate cleanup.

| Historical runner scenario | 0e22fdc and 31616be | 06b5af4 |
|---|---|---|
| Ordinary MPI exit across clock step | Generic refusal, no signal | Completed, status 0, no signal |
| Real timeout across clock step | Generic refusal, no signal | Timeout, status 124, intended subgroup TERM |
| Persistent denied exit probe compared with PID reuse | Same generic refusal for both, no signal | Same runner defect |
| Persistent denied probe during timeout cleanup | Generic exception, no signal | Same runner defect |
| Whole Boolean check fails, then recovers during ordinary drainage | Stable clock: unsolicited TERM. Stepped clock: boot mismatch masks the defect | Unsolicited TERM with either clock |
| One denied kernel read, then ordinary drainage | Stable clock: completed, status 0, no signal. Stepped clock: boot mismatch | Completed, status 0, no signal with either clock |

The matched synthetic comparison used psutil 7.2.2 on Python 3.11 and 3.14. It recorded signal requests without delivering them. The final adapter makes at most two complete observations, with zero backoff and a 100 ms monotonic deadline to start the second observation. In the 15 ms outage control, the earlier 10 ms sleep in b845df8 placed the second cleanup check after recovery and caused an unsolicited TERM. Removing the sleep restored the historical outcome (no signal) for that control.

This table records the historical comparison, not the behavior of R. The
[pinned compatibility test](../../tests/jobs/test_historical_vasp_runner.py)
retains `51d8209` as a negative control and pins R separately. The four cases
formerly marked as expected failures now assert the repaired behavior normally:
the two persistent-denial outcomes and recovery during ordinary drainage at
stable and stepped clocks. Persistent denial retains supervision without a
completion or signal; the bounded test horizon is not permission to terminate.
The CMW fixture suite passed 9 tests on each of Python 3.14 and 3.11: eight
ordinary positive assertions and one negative-control test confirming all four
historical failures in `51d8209`, with zero expected failures in the suite.

## Implemented port ownership contract

[Issue #19](https://github.com/angzeli/computational-modelling-workflow/issues/19)
tracked the separate port repair, implemented in R. The original change was
based on `51d8209`; that source is retained above as historical evidence.
The implemented contract resumes the original drainage observation after
recovery. Only an actual termination intent may start signalling cleanup.
A failed identity lookup cannot supply that intent.

1. Require the qualified CMW v2 identity and tri-state API at managed launch.
   Capture the leader once. Verify the session, protected group membership and
   sole foreground payload before spawning. Refuse launch if any check fails.
2. Replace the Boolean check in `managed_members` with explicit outcomes.
   Only VERIFIED permits the bounded membership scan. Treat failed topology
   reads as UNVERIFIABLE. Recheck identity and topology at the boundary of each
   decision and again inside each signalling helper. Preserve the protected
   leader exclusion and individual subgroup identity checks.
3. While ownership is UNVERIFIABLE, keep supervising the existing child and
   record the deciding check. Retry with bounded backoff. Retain pending
   termination intent without sending a signal, reporting drainage, publishing
   completion or switching to unmanaged cleanup. This includes the timeout,
   MPI-exit, exception and coordinated-control paths. After VERIFIED, repeat the
   membership scan and evaluate the pending operation with fresh evidence. GONE
   refuses further group control and records unresolved descendants. A wall-clock
   step alone has no effect on this state machine.
4. Qualify against a synthetic MPI family: forward/backward wall steps,
   cross-interpreter checks, denied reads before TERM and KILL, recovery,
   confirmed PID reuse and protection of the leader group. Verify that no
   signal reaches the replacement identity, and that coordinated mode leaves
   termination authority with its CMW worker. The CI evidence above covers the
   synthetic contract. Node qualification still uses the D32 sandbox and the
   explicitly selected D30 interpreter before a D31 production snapshot.

This plan applies to future launches. Recovery of an existing anonymous control
pipe requires a separate design.
