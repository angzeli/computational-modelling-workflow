# CI implementation and diagnostics

The [CI tier contract](../../docs/development/ci-tiers.md) defines scheduling and
coverage. [paths.json](paths.json) is the single path-policy source; [plan.py](plan.py)
selects jobs. This guide contains implementation detail, not additional merge gates.

## Suite and matrix runners

[run_tests.sh](run_tests.sh) discovers every test module, runs an isolated process
per module with bounded parallelism, and reports per-module and aggregate counts.
The ordinary suite uses the representative ownership-fault slice.
[run_ownership_sweep.sh](run_ownership_sweep.sh) runs the full unchanged matrix;
[check_ownership_sweep.py](check_ownership_sweep.py) requires an exact union of
unfiltered baseline read occurrences × fault types, with zero missing, extra or
duplicate rows. Report unittest methods and injected rows separately.
[check_artifacts.sh](check_artifacts.sh) validates fresh core wheel, Jobs wheel,
sdist and editable installations outside the checkout. Publication hygiene also
screens tracked source and built artifacts for private identifiers.

## Watchdog and local verification (unchanged semantics)

Ordinary CI test methods retain the 300-second native watchdog and the positive,
finite `CMW_TEST_TIMEOUT_SECONDS` fixture override. The budget includes setUp,
body, tearDown and instance cleanup, not module/class fixtures. Fault-sweep
lifecycle methods retain the stricter per-row watchdog; manifest checks use the
ordinary guard. A timeout prints the active test and stack, aborts that isolated
module process and may bypass cleanup. Partial counts are incomplete, never a
passing suite. The external reporter lists registered fixture PIDs and recorded
v2 identities; it neither discovers all descendants nor signals other processes.

Keep worktree-owned environments and low-priority sequential targeted checks
while calculations run (about ten minutes or less per interpreter). No full
local suite during a calculation. Audit registered identities and clean up
survivors only after ownership verification. Intentional indefinite
UNVERIFIABLE holds can turn a stale mock into a hang; inspect the actual caller
before changing assertions. Unqualified platforms are findings, not merge gates.

Failure triage and the "Never" rules are in [AGENTS.md §5](../../AGENTS.md#5-tests-and-ci).
