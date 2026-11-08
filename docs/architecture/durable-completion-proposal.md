# Durable completion — proposal

**Status: proposal only; post-cutover; reassess after the slimming audit.**
This document records the deferred PR B design from the issue #22 recovery work.
It authorizes no deployment, queue migration, process adoption or runner change.
The implementation preceding it retains completion in the original worker's
memory, keeps the current queue schemas and runtime protocol, and does not
recover completion after that worker dies.

Tracking: [PR B, issue #23](https://github.com/angzeli/computational-modelling-workflow/issues/23).
The related [first-domain handshake, issue #24](https://github.com/angzeli/computational-modelling-workflow/issues/24)
must guarantee that a short runner cannot finish before its first live domain
is verified. Both affect the release protocol and are post-cutover proposals to
reassess together after the slimming audit. PR A's never-observed-domain case
remains pending with manual verification, no signals and no RELEASE; see the
[operator procedure](../cmw-jobs.md#operator-procedure-for-the-never-observed-domain-hold).

## Principle and boundary

After RELEASE, completion records evidence already established by the original
attempt: its payload receipt, complete drainage census and release. A transient
read failure can delay publication; it cannot invalidate that evidence. Follow
the [shared unavailable-evidence rule](../periodic/vasp-result-evidence.md#unavailable-evidence).
Unavailable is never contradiction. Elapsed time does not turn uncertainty into
Unknown or authorize another signal.

Separate live process-control authority from authority to finish an already
completed attempt. A completion-only finisher can read evidence and commit a
terminal result; it cannot signal, send GO or RELEASE, adopt a process, restart
the computation, or reinterpret a new process with a reused PID as the original.

The current worker [release path](../../src/cmw/jobs/runtime.py) sends RELEASE,
reaps its anchor, checks final absence and commits completion. The
[payload shell](../../src/cmw/jobs/payload.sh) retains the anchor on guardian EOF
and exits when it consumes RELEASE. Pipe delivery and a worker-side durable
"sent" record cannot be atomic. A worker-written flag alone cannot prove that
the anchor received RELEASE.

## Proposed records and durable publication

Place versioned records in the existing private attempt namespace:

```text
attempts/<attempt_id>/completion/
    prepared.json
    released.json
    verified.json
```

Authoritative records are immutable, keyed by a unique completion ID. Keep
diagnostic phase/retry information separate from the evidence it describes.

| Record | Required contents |
| --- | --- |
| `prepared.json` | Record version and completion ID; queue/database identity; job, attempt and claim; original worker and anchor v2 identities; accepted cancellation or timeout intent and revision; proof variant; receipt relative path, exact-byte SHA-256, byte count and parsed contents when applicable; complete drainage census; retained original identities and groups; boot-session and monotonic observation stamps; wall times for display; frozen disposition and available accounting evidence. |
| `released.json` | Anchor-authored acknowledgement after it consumes the bound RELEASE: completion ID, prepared-record digest, anchor identity and observation stamp. This record is required only for the receipt/release proof variant. |
| `verified.json` | Binding to prepared/released evidence; final original-identity and group-absence evidence; receipt verification where applicable; terminal disposition; frozen timing evidence and any explicitly unknown timing fields. |
| Queue row | Pending completion ID, prepared digest and phase; after terminal commit, retain the completion ID and final evidence digest for idempotent outcome readback. |

Use a mode-0600 temporary file in the same mode-0700 private directory, flush
and fsync its contents, publish atomically, then fsync the directory. Receipt
publication must also establish directory durability before the receipt's hash
is accepted. The general provenance writer currently supplies atomic visibility
but is not by itself a directory-durability guarantee; reuse the descriptor-relative
private-file pattern from the Jobs suspension channel rather than weakening it.

Reads must be bounded, descriptor-relative and protected against symlink,
ownership and directory-identity substitution. An identical repeated publication
is a successful no-op. Different content under the same immutable completion ID
is a contradiction. A hash is integrity evidence, not independent authority: the
attempt/claim, queue identity, original owner and protected namespace remain part
of every check.

## Proof variants

`receipt_release` requires the genuine receipt, its integrity hash, complete
pre-release drainage, the anchor's durable release acknowledgement and final
absence evidence. It must not release resources from the payload exit code alone.

`forced_termination` preserves the existing ordinary hard-cancellation contract:
the original worker can complete from established termination intent, signal
delivery evidence and confirmed reap/absence without a payload receipt or RELEASE.
Retain the original identities and groups and distinguish delivered, denied and
unknown signal outcomes. Recovery never fabricates a receipt, assumes KILL was
delivered, or repeats a signal. An uncertain delivery or incomplete closure proof
remains pending. The proof and worker-death windows for this variant require
qualification alongside the receipt/release route.

Pre-admission refusal remains a separate path: it cannot be represented as an
executed payload's receipt or successful scientific result.

## Proposed state transitions

| Phase | Required evidence | Permitted next action |
| --- | --- | --- |
| `SUPERVISING` | Original live authority | Observe execution; preserve driven holds on unreadability. |
| `PREPARED` | Durable preparation plus matching queue seal | Original worker performs fresh final authority/intent/drainage checks, then sends the bound RELEASE once. |
| `AWAITING_RELEASE_ACK` | RELEASE attempted; acknowledgement not yet available | Read-only acknowledgement retries; no resend. |
| `VERIFYING_COMPLETION` | Valid release acknowledgement, or the forced-termination proof | Verify the original identities/groups are absent and required records remain consistent. |
| `COMMITTING_COMPLETION` | Durable verified evidence | Compare-and-commit the original attempt's terminal result. |
| `COMMITTED` | Matching completion ID/digest on the terminal row | Acknowledge success without another mutation or terminal event. |
| Any pending phase with prolonged unavailability | Evidence retained; retry driver active | Coalesced attention event, continued retries and retained commitments. |
| Contradictory evidence | Explicit conflicting claim, record, identity binding or terminal outcome | Refuse automatic completion and record the contradiction. |

The queue's prepared-record commit seals the accepted disposition. Cancellation
accepted before that point participates in the result. Later controls refuse
with a visible completion-pending reason rather than entering a new signal path.
No live control operation is replayed by completion recovery.

Backoff starts at 0.25 seconds and doubles to a five-second cap. Each scheduled
attempt has bounded record sizes, nonblocking lock acquisition and a short
SQLite busy timeout, with no nested indefinite retry. A cooperative per-attempt
budget is not a guarantee of interrupting a kernel-blocked filesystem call.
After 60 seconds, report attention without discarding evidence or changing
unavailability into Unknown. If the queue is unreadable, retain local diagnostics
and attempt evidence; failure to publish a diagnostic cannot end the retry driver.

Freeze timing from the first established completion evidence, separately from
its eventual database publication time. Do not let delayed database access
inflate execution duration. If a worker crash prevents exact exit timing from
being established, retain explicit timing uncertainty rather than inventing it.
Boot changes must never be handled by comparing monotonic values across boots.

## Idempotent completion and controller recovery

The terminal job update, failure-policy update, completion ID and terminal event
must commit in one SQLite transaction. After an ambiguous COMMIT, reopen and
read back before writing:

- Matching completion ID and digest already terminal: return success, without
  repeating the mutation or event.
- Same attempt and claim still pending: retry only the completion transaction.
- Temporarily unreadable result: remain pending and retry.
- Conflicting attempt, claim, evidence digest or terminal disposition: report
  contradiction; do not overwrite it.

Controller reconciliation checks supported completion evidence before its
legacy worker-loss handling. After the original worker is confirmed GONE and
the existing worker lock is available, the controller can claim a completion-only
finisher. Keep the original worker identity and claim as evidence; acquiring
this lock does not enroll a replacement supervisor or adopt the anchor.

Receipt reads, hashing, process observations and backoff occur outside the
controller's queue transaction. Only a short, freshly fenced compare-and-commit
runs inside it. Concurrent finishers must converge on the same committed result
with one terminal mutation/event. A new unrelated process using the old PID or
numeric group is never signalled and never substitutes for the original identity.

## Worker-death table and explicit pre-RELEASE limit

The anchor must write `released.json` after consuming RELEASE, before exit,
retrying publication while retaining its TERM/INT protection. The acknowledgement
binds the exact prepared evidence; a replacement finisher only observes it.

| Durable state when the original worker dies | Recovery |
| --- | --- |
| Prepared only; RELEASE not consumed | Remain visibly pending. The anonymous stdin pipe cannot be recovered, and another worker must not send a replacement RELEASE. |
| RELEASE consumed; acknowledgement publication pending | Anchor continues publishing the acknowledgement and then exits; finisher waits. |
| Acknowledgement durable; anchor exiting | Retry read-only absence observations. |
| Acknowledgement and verified closure durable | Controller or completion-only finisher can commit without `reconcile --apply`. |
| Terminal COMMIT succeeded but its result was lost | Matching completion ID/digest proves success; no mutation replay. |

If automatic recovery is required for the **before-RELEASE** window, a later
design must make a durable release-authorization record the authoritative
operation and let the anchor consume it independently of the lost pipe. That
is a different protocol, not a "sent" flag, and is explicitly not included in
this minimal acknowledgement proposal. Missing acknowledgement never proves
release; legacy receipts alone never grant recovery authority.

## Proposed compatibility fence

This design proposes **queue schema 5**, **worker/controller runtime protocol 4**,
and **completion record schema 1**. None is implemented by PR A.

Keep `Run`/`Cancelling` resource-committed with versioned completion metadata,
rendered as COMPLETION_PENDING. This avoids inventing a terminal status for an
uncommitted result, but still needs the schema fence: old runtimes would otherwise
ignore the metadata or mark a dead worker Unknown. Old readers must refuse schema
5 rather than silently control it.

Preserve enrolled queue identity as mandatory through an explicit local/enrolled
distinction; do not make node identity optional merely because schema 5 also
supports local queues. Completion metadata participates in node control revisions.
Lifecycle, portal, provenance and strict node readers must understand the new
records. Pending completion is neither successful reuse evidence nor invalidity.

Read old schemas unchanged. Never upgrade a live or unresolved queue implicitly.
Activation on an existing queue requires a separately approved, quiescent cutover.
No current J4.1/J27.1 state, deployment, enrollment or registry is changed by
recording this proposal.

## Qualification before implementation is accepted

Extend the single-fault read-site manifest to these new records and recovery
reads. For normal completion, cancellation and timeout, inject one failure at
each applicable read occurrence and require bounded eventual completion, no
additional signals, no duplicate GO/RELEASE, and exactly one completion mutation
and terminal event.

Add crash/race fixtures before/after prepared publication and queue sealing,
during RELEASE delivery, before/after acknowledgement publication, during final
absence verification, and around ambiguous COMMIT. Cover two competing finishers,
altered receipts, changed claims/queue identities, previous boots, unavailable
diagnostics, the forced-termination variant and every row of the worker-death
table. Separate process-death tests from transient-read tests so neither claims
the other's guarantees.

Use deterministic clocks and modeled processes for the large matrix, plus a
small real pipe/SQLite crash-window suite with unconditional owned-process cleanup.
Report both unittest counts and injected/crash-row counts. Full qualification is
CI's responsibility; no calculation or deployment is needed to test this protocol.
