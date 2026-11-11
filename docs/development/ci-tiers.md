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

## Console diagnostics

Successful cases do not stream STARTED/FINISHED records. Module counts and
private raw logs retain them for incomplete-module accounting; failed modules
print their diagnostic log and invoke the fixture-process reporter once.
The dedicated sweep uses the same bounded direct-child output reader. Its
console retains failed rows, watchdog evidence and the summary, including
scenario timings. The outer reader keeps the last 16 row starts in memory and
dumps them on a watchdog or native timeout, even if the test interpreter cannot
run Python callbacks. Full row evidence and the exact-union check are unchanged.
Private raw logs still contain all row markers; the reduction is in CI console
output, not a claim that private diagnostic files have been removed.

Publication hygiene also rejects potential-dataset table/header markers in
content, including innocuously named tracked files and artifact members. It
prints the path and a redacted reason only, never matched bytes. This bounded
screen supplements filename checks; it is not a general licence or secret scanner.

## Test diagnostics and triage

Watchdog budgets, fixture-PID diagnostics, stale-mock guidance and the triage
procedure live in [scripts/ci/README.md](../../scripts/ci/README.md).
The sole known-flaky registry is [tests/KNOWN_FLAKY](../../tests/KNOWN_FLAKY).
Issue creation follows [AGENTS.md §6](../../AGENTS.md#6-issues-and-scope).

## Measurement

Measured on 9 November 2026, the median ordinary non-critical PR path is about
9.8 minutes including selection and lint, versus 26.7 minutes for successful
first-attempt PR workflows before the tiers. Under ten minutes is a target, not
a guarantee; no watchdog budget or assertion was reduced to meet it.
