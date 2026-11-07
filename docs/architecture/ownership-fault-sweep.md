# Ownership observation fault sweep

This is the executable verification manifest for the in-memory ownership-hold
and completion recovery change. It does not claim worker-crash recovery; that
requires the [durable-completion proposal](durable-completion-proposal.md).

## What the matrix executes

`tests/jobs/test_ownership_fault_sweep.py` has fifteen lifecycle cases: ordinary
process group, owned session, legacy v1 owned session, coordinated direct tree,
and coordinated ORCA tree, each with normal completion, cancellation and timeout.
Each case first records a fault-free baseline. The full mode then runs a fresh
fixture for **every baseline read occurrence × EAGAIN, EPERM, UNAVAILABLE**.
Repeated reads at one source line remain separate rows. Exactly one observation
fails per row; all later observations use their original results.

The worker, SQLite queue, ownership classification, group/session census,
Coordinator, RunnerControl, private channel readers and ORCA completion verifier
execute their real code. Native process evidence, external-activity
admission and time advancement are modeled. No engine, subprocess or real signal
is launched. Each fixture owns a temporary queue and input/receipt directory.

The successful terminal class requires the expected status and exit code, a
recorded completion, no remaining ownership hold, a receipt, one GO, one RELEASE,
one terminal write and event, and the same nonzero signal trace as the baseline.
Its virtual-time limit is 30 seconds.

A separately counted class covers the approved pre-RELEASE limit: a receipt
arrives before any coordinated process domain was verified. It requires the
exact visible reason
`process domain never observed; receipt present; manual verification required`,
retained Run state, an open control pipe and live anchor, no Unknown transition,
no signal or RELEASE, no terminal mutation, and one coalesced attention event.
The fixture captures the live worker after 65 virtual seconds, before its
test-only stop unwinds cleanup; its bound is 75 seconds. It never turns missing
historical domain evidence into proof of safe release.

## Read-site inventory and limits

`tests/jobs/ownership_read_manifest.py` builds a lexical inventory of the six
requested modules and rejects a newly unclassified read scope. The report marks
whether each candidate call was reached in a fault-free baseline. A lexical
entry is an inventory entry, not a claim that the corresponding lifecycle was
executed. Composite calls such as `read_domain`, `wait` and `ack` delegate to
instrumented leaf readers; some contain no independent I/O boundary.

The injection wrappers cover imported ownership/group/identity observers,
Store get/control/rows/snapshot, read-only Path.open and file reads, read_text and
read_bytes, stat/lstat/fstat/readlink, read-only descriptor opens, process
identity/status/topology and enumeration, zero-signal group probes, the
non-mutating lock-held probe, child poll/wait, and clock/boot readers. Write
opens, nonzero signals, exclusive-lock acquisition and release are effects,
not invented read-error injection sites. The audit specifically added readlink
and lock-held's native flock probe; wrapping only their higher-level callers
would not exercise those native error classifiers.

| Not credited as a worker lifecycle row | Separate evidence / reason |
|---|---|
| Native identity-capture implementation | Process identities are fixture inputs. `test_process_identity.py` and `test_ownership_observation.py` test the native reader and v1/v2 result contracts. |
| OS-specific ancestry and descendant evidence | The fixture supplies native parent/children/start evidence. The real edge/token consistency algorithms and suspension descendant classification run, including a child below the computational root. OS readers retain their separate native contract tests. |
| Denied signal followed by witness exit; recovery-only hold publication/readback; admission refusal; missing-domain pending driver reads | These require an earlier fault, effect or changed scenario. Focused cancellation, hold and completion tests cover them; they are not silently counted as fault-free-baseline occurrences. |
| Suspend/continue, stopped/running transition and timing-only paths | Separate state/protocol tests; a normal/cancel/timeout fixture does not issue a suspend or continue request. |
| Controller start, reconciliation, enrollment, UI readiness and observation-only entrypoints | Separate lifecycle/clock/state tests. They do not themselves own a normal/cancel/timeout worker completion. |
| ORCA scientific preparation, MPI qualification and external driver startup | Existing ORCA/protocol contract tests. The sweep exercises the ORCA completion handoff, without launching or qualifying an engine. |
| Ambiguous COMMIT and committed-write readback | `test_completion_retry.py` covers idempotence directly; these are not native read rows discovered from a fault-free lifecycle. |
| Executable PATH search and initial context publication failures | Dedicated preflight and constructor tests cover these boundaries. The read matrix does not inject write, fsync or publication failures; terminal-write uniqueness does not establish idempotence for every setup mutation. |
| Raw zero-argument `time.time()` / `time.monotonic()` | Their supported CPython calls do not expose EAGAIN/EPERM. The first-boundary clock/timestamp failure robustness cases are separate tests in `test_completion_retry.py`; the matrix injects into the typed `now_clock`/clock-descriptor evidence boundaries. |
| Worker death and controller takeover | Explicitly outside this change; see the durable proposal. |

The named focused suites establish their own assertions. They are **not**
claimed to execute the entire three-error lifecycle cross-product. The dynamic
report exposes all unreached inventory calls, including alternate branches.

When a completion-boundary clock is unavailable, a terminal row must retain the
specific diagnostic event, uncertain accounting, no invented `clock_finished`,
and an unverifiable duration. A missing wall-clock completion timestamp is
accepted only with its specific diagnostic and an elapsed value that remains
unknown when display time advances. Missing completion fields are not generally
accepted.

## Running and reporting

The diagnostic runner streams a start/end record for each row, including its
scenario, exact read occurrence and fault, completed-row count and real elapsed
time. Each scenario and test module also emits a timing/count summary as soon as
it finishes; it does not wait for unrelated modules. Complete sweep output,
including watchdog stack traces, is retained and streamed by the existing CI
runner. A missing final summary means incomplete evidence, never zero tests or
a pass.

Every baseline and injected row has a 10-second real-wall watchdog, covering
fixture setup, execution, assertions and cleanup. This is deliberately generous
against ordinary rows taking well under a second. A native 20-second hard-stop
fallback covers a blocked C call or stuck cleanup. The already-existing
30-second virtual limit (75 seconds for the accepted never-observed-domain case)
and 50,000-read-operation bound remain unchanged. Exceeding any bound reports
the exact row, last observed read, elapsed time and budget, then stops the sweep;
it is a liveness failure, not a row to skip or retry. The watchdog uses real
clock/signal functions outside the fixture's patched observations. Its signals
target only the synthetic test interpreter, never a worker or calculation.

This instrumentation changes neither the selected rows nor their assertions,
CI parallelism or the 45-minute suite limit. CI restructuring requires evidence
that the aggregate duration, rather than a hanging row, caused the limit.

Full discovery uses the complete matrix by default; no CI-specific skip or
new workflow is needed. For proportionate sequential local checks:

```sh
CMW_FAULT_SWEEP=representative \
CMW_TEST_EVIDENCE_DIR=.venv/diagnosis/ownership-sweep \
nice -n 19 .venv/py314/bin/python -B -m unittest tests.jobs.test_ownership_fault_sweep
```

Representative mode selects the first reached occurrence of every distinct read
API in each lifecycle. This reduces local cost only; it is not the full matrix.
The output gives unittest counts separately from attempted, passed and failed
injected rows, and splits terminal rows from the accepted pending class. With
`CMW_TEST_EVIDENCE_DIR`, JSON records the dynamic sites/occurrences, source
inventory, limitations and each row's result. Assertions also detect an injection
that never fired or fired more than once.

The representative checks passed on both local interpreters:

| Interpreter | Unittest count | Injected rows | Terminal / pending | Elapsed |
|---|---:|---:|---:|---:|
| Python 3.14 | 16 | 1,275 | 1,217 / 58 | 40.407 s |
| Python 3.11 | 16 | 1,275 | 1,217 / 58 | 44.666 s |

The source inventory has 366 candidate calls; 156 were reached by the fault-free
baselines. That distinction is intentional: inventory entries include composite
calls and the explicitly excluded scenarios above. Native path depth and
interpreter layout can change the full row count across platforms. Full-matrix
and CI results belong in the PR validation report; this document asserts only
the local representative results.
