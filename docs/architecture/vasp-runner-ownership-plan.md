# VASP managed foreground ownership qualification

The VASP port owns MPI supervision in `scripts/launch.py:supervise`. The file
is identical at `f7afe0a` and the D31 base `51d8209` (Git blob `5a51f95`).
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
only reads evidence. A final false
result can mean either confirmed loss or exhausted retries. Callers that need
to distinguish these outcomes use `observe_owner` and own their retry policy.

## Separate port change based on 51d8209

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
   termination authority with its CMW worker. Qualify in the D32 sandbox with
   D30's interpreter pin before creating a new D31 snapshot.

This plan applies to future launches. Recovery of an existing anonymous control
pipe requires a separate design.
