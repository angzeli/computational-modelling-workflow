# Retiring an Unknown attempt whose processes are gone

`cmw jobs --state QUEUE_STATE retire JOB --reason TEXT [--json]` is the
supported local recovery for an `Unknown` attempt after its recorded process
authority and execution domain have ended. It does not determine why the
calculation ended or whether its scientific outputs are acceptable. Review
those outputs independently before deciding to retire the record.

The operation is local only: `--node`, remote queues and the SSH endpoint do
not provide retirement. Select the existing queue; do not enroll a replacement,
copy the database or edit its rows. Node, queue and storage identities are
unchanged (D01), including an enrolled schema-4 queue written by runtime
`31616be`.

## Safety and result

Retirement requires all of the following under the existing queue locks:

- The selected job is `Unknown`, with the same immutable attempt binding.
- Every recorded process identity is GONE under the existing tri-state reader:
  worker, recorded controller owner, anchor/group, runner, MPI and recorded
  descendants or attempt members. VERIFIED means the recorded process still
  exists; UNVERIFIABLE is not proof of absence. Either refuses retirement and
  identifies the evidence and reason.
- No live member remains in a recorded session or process group. Missing,
  malformed or unreadable evidence cannot grant permission.
- A recorded anchor/group identifies the execution domain. A spawn crash that
  never recorded that domain cannot be retired by this command; it still needs
  separately qualified recovery rather than an invented identity.
- The relevant ownership locks are free. Stop the controller through its
  existing supported command before retirement; a running controller is not
  overridden.

A successful operation uses one transaction to record the reason, operator,
time and full GONE evidence snapshot and move the job to the existing terminal
status **Fail**, explicitly marked as administratively retired with execution
outcome unknown. `Fail` is used because retirement establishes neither a zero
exit nor a cancellation performed by CMW. It fabricates no exit code or signal
and performs no scientific finalization or result verification. The original
job and attempt stay in history; other jobs are unchanged.
The retirement timestamp is separate from execution time: `finished_at`,
exit code and signal stay unknown, so retirement does not invent a duration.

Retirement sends no signal, changes no job input/output file and does not
release an anchor. It removes this attempt's admission block but does not start
a controller or enable dispatch. After reviewing the remaining queue, the
operator separately starts the controller or resumes dispatch. Other Unknown,
Cancelling or held records and ordinary admission checks still apply.

A repeated request for an already-retired attempt reports that fact without
writing another event or changing the original reason or evidence. A refusal
changes nothing. Exit 0 reports retirement or this idempotent result; exit 75
reports UNVERIFIABLE evidence; exit 2 reports a live owner or another failed
precondition. None is an endpoint protocol code.

## Reboots, PID reuse and legacy v1 records

| Recorded evidence | Observation | Retirement |
| --- | --- | --- |
| v2 identity from a different boot session on the same host | GONE before probing the recorded PID; an earlier boot's session/group cannot survive the reboot | May proceed if every other precondition passes; a reused numeric PID on the new boot is not the old process |
| v2 identity on the same boot, different raw start token | GONE for that recorded identity | Still requires the recorded session/group to be empty |
| v1 identity with matching boot evidence and a confirmed absent PID | GONE | May proceed if every other precondition passes |
| v1 boot timestamp differs, including after a reboot | UNVERIFIABLE before a process-absence probe | Refuses, even if the operator knows a reboot occurred |
| v1 adjusted birth differs, including PID reuse | UNVERIFIABLE | Refuses; do not kill the reused PID or rewrite the identity |

These are the PR #18 rules in [`observe_owner`](../src/cmw/jobs/ownership.py).
A v1 wall-clock boot timestamp can change without a reboot, as the
[J4.1 incident](incidents/2026-11-j4.1-clock-step.md) demonstrated. A timestamp
ordering, elapsed time, empty process listing or new owner at the same PID
cannot distinguish that case from a reboot. Retrying may resolve a transient
read error; it cannot turn a persistent legacy mismatch into GONE. Preserve the
queue and original evidence, keep dispatch paused, and request an explicitly
reviewed recovery for that case. There is no force flag or in-place v1 upgrade.

**Proposal only — provably earlier v1 boot.** A future exception could accept a
trusted boot-transition attestation binding the exact host, queue, attempt and
v1 identity records to a known previous kernel boot UUID, then verify a
different current kernel boot UUID on that same host. The binding must come
from independently trustworthy pre-reboot evidence, not a timestamp inferred
from the v1 record or an operator's unsupported assertion. A controlled future
reboot could capture and protect that evidence beforehand. Existing legacy
records lacking such evidence cannot be repaired by inventing it after the
fact. This exception needs a separate design and qualification; retirement
does not implement it.

## Which current paths create Unknown

The following audit is pinned to base main `66a372a`. Retirement is the
supported administrative recovery for these cases once its GONE and empty-domain
preconditions hold; it does not recover a live worker or validate completion.

| Trigger | Current behavior and source |
| --- | --- |
| Recorded worker is confirmed gone | Controller reconciliation records Unknown and pauses dispatch: [`runtime.py:181–188`](https://github.com/angzeli/computational-modelling-workflow/blob/66a372a/src/cmw/jobs/runtime.py#L181-L188). An unreadable worker instead enters a retryable hold. |
| Controller restarts after a reboot | The controller's normal tick reconciles active jobs ([`:308–310`](https://github.com/angzeli/computational-modelling-workflow/blob/66a372a/src/cmw/jobs/runtime.py#L308-L310), [`:356–368`](https://github.com/angzeli/computational-modelling-workflow/blob/66a372a/src/cmw/jobs/runtime.py#L356-L368)). An earlier-boot v2 worker is GONE, so the preceding rule applies. A v1 boot mismatch is UNVERIFIABLE and remains held; restart itself does not make that mismatch Unknown. |
| Workerless launch cannot establish its grace period, or verified worker authority lacks its lock | Missing/previous-boot `Starting` claim stamps become Unknown immediately; otherwise a workerless claim has a five-second same-boot grace before the unresolved path. A verified worker without its lock also takes the unresolved path: [`runtime.py:189–216`](https://github.com/angzeli/computational-modelling-workflow/blob/66a372a/src/cmw/jobs/runtime.py#L189-L216). |
| Readable evidence contradicts ownership/control | The worker catches `OwnershipContradiction`, rechecks claim and active status, then records Unknown: [`runtime.py:1073–1080`](https://github.com/angzeli/computational-modelling-workflow/blob/66a372a/src/cmw/jobs/runtime.py#L1073-L1080). |
| Anchor is confirmed gone during blocked internal recovery | The worker records Unknown only after GONE: [`runtime.py:1081–1097`](https://github.com/angzeli/computational-modelling-workflow/blob/66a372a/src/cmw/jobs/runtime.py#L1081-L1097). Unreadable observations retain the pipe and hold. |

A transient read failure, wall-clock step or completion retry does not by
itself authorize Unknown or retirement. See the
[ownership and recovery contract](architecture/multi-host-nodes.md#39-process-identity-and-supervision-d34).

## Mini J4.1 cutover step — procedure, not execution authorization

This step preserves Mini node `702779636fc14335bcf71c1395549f43`, queue
`2b975d09e4104933b7c1c146538fbc6b` and its existing storage identities. It must
run locally on the Mini only in the separately authorized cutover window, after
the research calculation has finished. This document does not authorize
contacting the Mini or acting on a PID now.

1. Independently verify and preserve the research outputs and the genuine
   execution evidence. `Unknown`, a receipt, or retirement is not a scientific
   acceptance decision. Keep dispatch paused and stop the old controller using
   its existing runtime; confirm that it has exited without altering any job
   state or queue identity.
2. Select the original queue state from its existing enrollment. Capture
   `show J4.1 --json`, `observe J4.1 --json`, and
   `ownership J4.1 --role group --json` using that explicit `--state`. Confirm
   the immutable attempt, host and original anchor identity, and inspect all
   recorded runner/MPI/descendant evidence. The incident recorded anchor PID
   **90838**; that number is a historical candidate, never authority by itself.
3. If the leftover anchor is still live, revalidate its complete recorded
   identity immediately before the separately authorized **SIGKILL of that
   anchor only**. Require VERIFIED, the expected anchor PID 90838, and the
   expected session/group binding; first establish that the computation and
   receipt writer have finished and no producer remains. If any identity is
   GONE or UNVERIFIABLE, do not signal that PID. Never signal a whole group or
   a reused PID merely to make retirement pass.
4. Wait for the anchor to be reaped. Confirm every recorded identity is GONE
   and every recorded session/group is empty. Any v1 boot/birth mismatch is the
   refusal described above; stop the cutover step rather than edit the DB,
   force a result, or reboot as a workaround.
5. With a qualified candidate installation, on the original queue run:

   ```sh
   cmw jobs --state "$QUEUE_STATE" retire J4.1 \
     --reason "Cutover: outputs independently reviewed; recorded execution processes confirmed gone" --json
   ```

   `QUEUE_STATE` means the existing state root whose queue UUID was checked in
   step 2, not a newly created state directory. Check the retirement event and
   retained evidence. The result is administrative `Fail`, with no claim about
   the scientific outcome.
6. Only after the rest of cutover qualification and queue review succeeds,
   explicitly authorize dispatch again. `cmw jobs --state "$QUEUE_STATE" start`
   starts a stopped qualified controller and permits dispatch if the remaining
   gates pass; `resume` is for an already-running controller. Retirement itself
   does neither. No queue or storage reenrollment is part of this step.
