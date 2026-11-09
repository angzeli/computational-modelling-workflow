# CI tiers and coverage contract

CI changes scheduling, not test meaning. The qualified combinations are Ubuntu
24.04 x64/Python 3.11 and macOS 15 Apple Silicon/Python 3.14. No test module is
removed or filtered from the full-suite runner. Scientific engines and production
nodes are never used by these jobs.

## Triggers and guarantees

| Tier or addition | Trigger | Jobs and guarantees |
|---|---|---|
| Fast | PR opened/reopened or a new head pushed | Lint/private-identifier publication screen, docs checks, Ubuntu full suite with representative fault slice and required SFTP integrations, core wheel. Target median under ten minutes; not a hard timeout. |
| Merge | PR opened non-draft, marked ready, labelled `ci:merge`, or a subsequent push while ready/labelled | macOS full suite, representative slice and required SFTP integrations. A ready/label event runs only the macOS suite, not another Ubuntu suite or wheel. |
| Ownership/process addition | A PR code event whose complete diff matches `fault_matrix` | Full unchanged fault matrix on **both** platforms, including the exact-union check. |
| Packaging addition | A PR code event matching `packaging` | Core wheel, Jobs wheel, sdist and editable smoke on **both** platforms, built from fresh installations outside the checkout. |
| Prose/site-only | Every changed path matches `documentation` | Lint, publication hygiene and offline docs checks only; no Python suites, matrices or install jobs. Machine-readable JSON contracts are not prose and do not receive this exemption. |
| Full | Every push to main, nightly at 02:23 UTC, CI-policy changes on a PR, or the `ci:full` label | Both full suites and representative slices, both full matrices, all four install modes on both platforms, required SFTP checks, lint/publication and docs checks. No docs-only exemption on main/nightly. |

**Deployment candidates must have passed a full nightly (or main) run at the
exact candidate commit.** A green fast or merge tier is not deployment
qualification. Native engine and node qualification remain separate gates.

A PR opened ready runs both fast and merge tiers immediately. Draft PRs defer
the macOS suite until readiness or `ci:merge`; new commits refresh applicable
checks. A `ci:full` label explicitly requests a complete run, even for docs, and retains
that selection on later pushes while the label remains.
There is no duplicate manual-dispatch Ubuntu suite: this workflow has no
`workflow_dispatch` trigger. Main and nightly runs are never cancelled by PRs.
The PR push concurrency lane cancels superseded push runs. Ready/label events
use separate lanes so that marking a draft ready cannot cancel its unfinished
Ubuntu check. Unrelated labels select no test jobs.

The planner runs against the complete merge-base-to-PR-head Git diff with full
history, NUL-delimited names and renames expanded to old/new paths. It has no
300-file API/filter truncation. Missing Git objects or malformed commit IDs fail
the plan; an empty diff does not grant the docs exemption. The job summary lists
the paths and selected matrices. Tests run on GitHub's checked-out PR merge ref.
No `pull_request_target`, elevated token or execution of PR text as shell is used.

## One path-policy source

[`scripts/ci/paths.json`](../../scripts/ci/paths.json) is the only authoritative
path list, read by [`plan.py`](../../scripts/ci/plan.py). Patterns use Python's
case-sensitive `fnmatchcase` against repository-relative names (`*` can include
slashes). The families are:

- `full`: workflow files, CI scripts/configuration, and their routing/reporting tests.
- `fault_matrix`: ownership, hold, runtime, suspension, identity and runner modules
  and matching tests; lifecycle, store, process-domain helpers, payload and relevant
  historical fixtures and shell assets. These include the six swept modules and
  the pinned historical-runner compatibility tests.
- `packaging`: project/lock/build metadata, entry points, packaged shell/runtime
  assets, and installation/publication test drivers.
- `documentation`: prose guides, root documentation and the static website.
  Paths outside this allowlist always retain the normal code tier.

Update this file and its routing tests when moving a module or introducing a new
critical dependency. CI-policy changes select the full set so modifications to
the scheduler itself cannot validate only their reduced output.

## Coverage equivalence

| Existing module or job | Fast | Merge | Path addition | Main/nightly |
|---|---|---|---|---|
| Every `tests/**/test_*.py` (319 baseline modules; new routing tests add one) | Ubuntu, except docs-only | macOS, except docs-only | CI changes run both | Both platforms, unfiltered discovery |
| `tests/jobs/test_ownership_fault_sweep.py`, representative mode | In the ordinary suite | In the ordinary suite | Same | Both ordinary suites |
| Same module, full mode; `check_ownership_sweep.py` | — | — | Both platforms for critical/CI paths | Both; exact union, no shard/row filtering |
| Required SFTP upload and download | Ubuntu | macOS | Both for full requests | Both, `CMW_REQUIRE_SFTP_INTEGRATION=1` |
| `check_artifacts.sh`: core | Ubuntu | — | Both for packaging/CI | Both |
| `check_artifacts.sh`: jobs, sdist, editable | — | — | Both for packaging/CI | Both |
| `distribution_hygiene.py`, private-identifier/source screen | Every active PR event | Every active PR event | Artifact screen also retained | Every run and built artifact |
| Offline Markdown fences/local links and website local links | Every active PR event | Every active PR event | Same | Every run |
| Pages deployment | Unchanged: separate existing workflow on main | — | — | Main; existing manual Pages trigger unchanged |

The full suite still uses the unchanged `run_tests.sh`: one isolated process per
discovered module, bounded parallelism and its existing progress/count report.
The full matrix still uses the unchanged `run_ownership_sweep.sh` and checks
that the union equals all unfiltered baseline read occurrences × fault types,
with zero missing, extra or duplicate rows. Counts are platform-specific; report
unittest methods and injected rows separately. No workflow modifies the sweep
manifest or expected-failure assertions.

Pip caching stores downloaded packages/wheels only, keyed by the platform,
Python version and both `pyproject.toml` and `uv.lock` through `setup-python`.
Installations, environments, runtime evidence and test results are never cached.
The artifact driver still creates a fresh environment for every install mode.

The offline docs check validates tracked Markdown fence balance and relative
Markdown/HTML file targets. External URLs, site-root routes and fragment anchors
are not fetched or validated; tracked symlinks are refused without reading them.

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

### Triage before calling a failure real
1. **Load:** rerun the failing test alone. On its first occurrence, a failure
   that passes alone may be recorded as provisionally LOAD. If it recurs,
   investigate its cause before retaining the LOAD classification.
   In CI, a test recorded as known-flaky in an open issue (currently the study
   lifecycle timeout and the test_names.py restoration race) may be rerun once.
2. **Pre-existing:** if it fails alone, run it on the base commit with the same
   kind of environment. If it fails there too, it is pre-existing.
3. **Introduced:** fails only on the branch.

Only introduced failures block a merge, with one exception: a PR that changes
the CI test matrix must fix, within the same PR, any test that fails on the
newly gated combination, even if the failure is pre-existing. Record other
pre-existing failures and recurring load failures as issues, stating whether
they affect real behaviour or only tests.

### Never
- Change code or tests just to make CI pass without approval.
- Bypass checks or branch protection (`--admin` or equivalent).
- Treat a skipped, cancelled or unfinished check as passing.
- Report a suite as passing without test counts.

## Policy consolidation in AGENTS.md

Removed/replaced instructions, with their replacement:

1. Separate “Continuous integration” section and its repeated platform/lean-CI
   guidance → one CI/test-verification section and this contract.
2. “Ubuntu on every PR, macOS on main/manual” and the instruction to dispatch
   manual macOS before merge → automatic merge tier plus main/nightly full tier.
3. Full matrices on every workflow regardless of changed paths → critical-path
   additions and unconditional main/nightly matrices.
4. Repeated full-suite/local-selection/low-priority/environment rules → concise
   local rules in AGENTS.md, preserving the calculation boundary.
5. Long watchdog explanation → this linked section; implementation unchanged.
6. Scattered review/count/skip/protection rules → the single section, retaining
   the same obligations. Failure triage and the newly-gated-combination exception
   above are copied verbatim; no flaky rerun authority was expanded.

## Measurement and savings

See [the measured baseline](ci-baseline-2026-11-09.md). The median ordinary
non-critical fast path is about **9.8 minutes** including selection/lint overhead,
versus **26.7 minutes** for successful first-attempt PR workflows: about **63%**
less wall time. The observed Ubuntu suite tail is 12.1 minutes, so under ten is
a target rather than a guarantee. No watchdog budget or test assertion is reduced
to meet it. Ready macOS checks still add a median 13.6 minutes of execution plus
allocation; that work is delayed until readiness, not removed.

Each avoided full matrix saves roughly 30.4 Ubuntu runner-minutes and, when it
would otherwise run, 22.9 macOS runner-minutes. Removing a duplicate manual Ubuntu
suite saves roughly 9.6 runner-minutes per completed pair. Main/nightly install
coverage expands from core-only on main to all four modes: roughly 8.5/11.9
minutes instead of 2.4/3.2 minutes on Ubuntu/macOS. These estimates are not claimed
savings for ownership/packaging PRs, which retain their expensive coverage.
