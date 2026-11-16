# CMW agent rules

Generic working rules come from the global agent file; these are repository-specific additions and overrides.

## 1. Safety (mandatory)

## Production and sandbox boundary (mandatory; D32)

Each node has exactly one production configuration. All agents must follow the
canonical layout in [the multi-host nodes specification](docs/architecture/multi-host-nodes.md).
Do not create a new production enrollment, configuration, registry, runtime or
runner deployment outside that layout. Benchmarks and experiments must use a
separate sandbox namespace for their configuration and execution state and
must never target a production queue. This rule is effective immediately.

Never publish POTCAR content. Never disturb running calculations or infer execution permission from available resources.
Running calculations take priority: local tests must be sequential, at nice 19, and about ten minutes or less per interpreter. Never run a full local suite during a calculation.

## 2. Reuse-first routing

Consult the relevant task family in [CAPABILITY_MAP.md](CAPABILITY_MAP.md), its linked contract, and the actual CLI/API/source entry point before carrying out or implementing a computational workflow.
Use supported capabilities for usage requests; do not turn usage into development without an authorised, demonstrated gap.
Extend existing implementations rather than duplicate converters, builders, checkers, runners, schedulers or provenance models. Small adapters may compose existing APIs.
Do not bypass checks, silently choose scientific parameters or infer execution authorisation. Report contract drift and update the capability map when a boundary changes.

## 3. Shell/Python boundary

Shell owns operational orchestration: entrypoints, environment handling, executable discovery, process launch, descriptors, signals, traps, cleanup, temporary directories and sequencing Python calls.
Python owns reusable semantics: parsing, scientific validation, identities, provenance, artifacts, lineage, locking decisions, reuse, plans, dependency rules and structured results.
Use stable exit codes and machine-readable JSON between layers; do not duplicate ORCA validity, convergence, geometry identity or reuse decisions in shell.
Process health is advisory operational evidence, never scientific success or an automatic termination policy.

## 4. Git and PR exceptions to the global rules

Agents may commit, push and open a PR for authorised work; merging requires the owner's instruction. This permission does not authorise deployment or unrelated changes.
Folding corrections into the task's own unpublished commits is pre-approved. Rewriting published history still requires explicit permission.
Read and address or explicitly answer every review comment, including automated reviews, before reporting ready.
An authorised push/PR may proceed during targeted diagnosis; merging waits for the applicable checks. Wait for and report post-merge main CI. Bounded waiting for requested work is permitted; unrelated repeated process polling is not.
Use the issue and PR templates: summaries at most 600 characters; evidence in links or `<details>`.

## 5. Tests and CI

Minimum Python: 3.11; do not add 3.10 compatibility shims. Qualified combinations: README “Tested support”. CI supplies full-suite evidence.
Follow [the CI contract](docs/development/ci-tiers.md) and [implementation guidance](scripts/ci/README.md): fast PR checks, automatic macOS merge tier, path-triggered additions, and full nightly/main runs. Keep CI proportional; do not add unrequested gates.
**Deployment candidates must have passed a full nightly (or main) run** at the exact candidate commit; PR checks do not establish deployment qualification.
Use each worktree's own environment. Select changed-module tests, direct importers where few, and changed non-import entry points; do not expand through transitive imports.
Text-only changes need focused text/diff checks, not local test suites. Run a full local suite only when requested or justified by cross-cutting runtime changes and no calculation is running. Never run two full suites concurrently.
Audit registered fixture identities after local runs; clean up survivors only after re-verifying ownership.

1. **Load:** an isolated pass on a first occurrence is only provisional LOAD. Investigate causation on recurrence. Only exact failures in [tests/KNOWN_FLAKY](tests/KNOWN_FLAKY), linked to an open issue, have the existing one-rerun allowance.
2. **Pre-existing:** if it fails alone, compare the base commit in the same kind of environment.
3. **Introduced:** fails only on the branch.

Only introduced failures block merging, except that a CI-matrix-changing PR must fix failures on the newly gated combination even if pre-existing. Record other findings under §6; unqualified-platform failures are findings, not merge gates.
Never change code or tests merely to pass CI without approval, bypass checks/protection, count skipped/cancelled/incomplete checks as passing, or report a suite without counts. Report unittest methods separately from injected rows.

## 6. Issues and scope

Agent sessions may open an issue only for (a) a failure observed in CI or a
local run, with a link, (b) a blocker to authorised work, or (c) a
pre-existing failure found while triaging. Use the defect template;
evidence goes in links or `<details>`. Feature, enhancement, refactor and
"post-cutover" proposals are written as a short comment or note to the owner,
and become an issue only with the owner's decision. Do not open an issue for a
flake on its first occurrence. Record it in the existing flake issue, or
open one after the second occurrence.

Fix defects with the minimal change; introducing a new mechanism (a hold, retry, harness or protocol) needs the owner's decision first.
