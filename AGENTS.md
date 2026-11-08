# Universal Codex Working Agreement

## Objective

Complete the requested work correctly with the smallest sufficient scope, context, command count, and validation cost.

Default loop:

**locate -> understand -> edit -> targeted validation -> inspect diff -> stop**

These are defaults. More specific user, repository, directory, or task instructions take precedence.

## CMW reuse-first routing (mandatory)

Before carrying out or implementing a computational workflow/helper, consult the
relevant task family in [CAPABILITY_MAP.md](CAPABILITY_MAP.md), read its linked
contract, and verify the actual CLI/API/source entry point. For usage requests,
use the existing supported capability; do not turn usage into CMW development
without a concrete need and appropriate authorization.

For development, extend the relevant implementation instead of duplicating a
converter, potential builder, checker, materializer, runner, scheduler or
provenance model. Establish the unsupported requirement or contract mismatch
before proposing a new route. Small project adapters may compose existing APIs.
Do not bypass established checks with unreviewed equivalent commands, silently
choose scientific parameters, or infer execution authorization from availability.

The map is a routing index, not a replacement for source or feature contracts;
inspect and report drift. Read only relevant guidance and reuse established
context until requirements/code change; no full census for a typo or reread
before every command. Update the map with any actual capability boundary change.

## Production and sandbox boundary (mandatory; D32)

Each node has exactly one production configuration. All agents must follow the
canonical layout in [the multi-host nodes specification](docs/architecture/multi-host-nodes.md).
Do not create a new production enrollment, configuration, registry, runtime or
runner deployment outside that layout. Benchmarks and experiments must use a
separate sandbox namespace for their configuration and execution state and
must never target a production queue. This rule is effective immediately.

## 1. Scope and planning

- Identify the goal, constraints, likely affected files, and observable completion condition before editing.
- Proceed directly on small, well-scoped tasks. Use a concise plan only for genuinely multi-step or high-risk work.
- Do not expand into adjacent cleanup, redesign, migration, documentation, or feature work unless required.
- Ask for clarification only when missing information blocks safe or correct progress. Otherwise make the narrowest reasonable assumption and report it.
- Treat finishing efficiently as part of correctness.

## 2. Efficient discovery and context use

- Start from filenames, symbols, errors, tests, or directories named by the task.
- Search narrowly before reading broadly; read focused ranges and expand only when evidence requires it.
- Do not build a complete repository model for a local change.
- Exclude vendored code, build output, caches, generated artifacts, archives, and unrelated data from routine searches.
- Bound command output. Prefer filtered excerpts over full files, logs, or repository dumps.
- Do not repeat listings, searches, file reads, Git checks, or environment probes without a reason.
- Once evidence establishes a fact, reuse it. Recheck only after a change that could invalidate it.
- Do not narrate every command or paste large source sections into working notes or the final report.

Before an unrequested action, ask internally:

**Will this result plausibly change the implementation, validation conclusion, or final answer?**

If not, skip it.

## 3. Implementation policy

- Make the smallest coherent change that fully solves the task.
- Reuse existing helpers, patterns, configuration, and abstractions before creating parallel implementations.
- Preserve public interfaces, schemas, file formats, CLI behavior, and compatibility unless the task requires a change.
- Follow existing repository style and architecture.
- Avoid unrelated refactors, renaming, formatting, import churn, comments, and generated-file updates.
- Add abstractions, fallbacks, compatibility layers, or error handling only for demonstrated needs.
- Do not modify vendored, generated, lock, snapshot, or fixture files unless necessary.
- Comments should explain non-obvious intent or constraints, not restate code.

## 4. Validation policy

Validation must be proportional to scope, risk, and reversibility. The goal is sufficient confidence, not maximal activity.

- **Text-only change:** inspect the changed text and focused diff. Do not run tests unless the text is executable.
- **Small isolated code change:** run the directly relevant test, syntax/import/type check, or direct behavior check.
- **Multi-module change:** run affected tests and, when useful, one representative integration or smoke check.
- **Cross-cutting, release, migration, benchmark, or infrastructure change:** broader validation may be appropriate.

Rules:

- Do not run the full test suite by default.
- Run broader suites only when explicitly requested, required by repository policy, justified by cross-cutting risk, or needed after targeted checks reveal coupling.
- Use one check per distinct validation purpose; avoid equivalent repeated checks.
- Do not rerun an unchanged passing check unless relevant code, inputs, or environment changed afterward.
- Add or update tests when behavior changes, a bug needs a regression test, or risk justifies it; do not add tests mechanically.
- Do not fix unrelated failures unless requested. Distinguish pre-existing failures from regressions introduced by the task.
- Do not claim validation that was not performed. If a check cannot run, report the exact limitation and strongest available substitute.

## 5. Hashing and integrity

Hashes are exceptional, not a routine coding check.

- Do not compute repository-wide hashes, tree manifests, file inventories, or repeated checksums for ordinary work.
- Prefer focused `git diff`, `git status --short`, direct inspection, and targeted tests.
- Use hashes only when exact identity or integrity is part of the task, such as immutable inputs, release artifacts, benchmark fixtures, corruption detection, reproducibility, or user-requested checksums.
- Hash only the smallest relevant file set and normally only once per needed state.
- Do not hash large unchanged files merely to make validation appear stronger.

## 6. Command and dependency discipline

- Prefer repository-provided scripts and the existing package manager, environment, and toolchain.
- Use targeted commands with bounded scope and output.
- Do not install, upgrade, downgrade, or replace dependencies unless necessary.
- Avoid broad dependency updates when a narrow change is sufficient.
- Use network access only when the task genuinely requires external or current information.
- Do not start persistent servers, watchers, daemons, or background jobs unless requested or necessary.
- Do not repeatedly poll a process; take one status snapshot unless monitoring is requested.
- Avoid destructive commands and remove only temporary files created by the task when safe.

## 7. Git safety

- Treat pre-existing changes as user work unless clearly proven otherwise.
- Never discard, overwrite, reset, clean, stash, or revert unrelated changes without explicit instruction.
- Do not use destructive shortcuts such as `git reset --hard` or `git clean -fd` on user work.
- Inspect Git state only as much as needed to protect existing work and review the task diff.
- Stage or commit only when requested; stage only the intended scope and inspect the staged diff first.
- Do not amend, rebase, push, create a pull request, tag, or alter remotes unless explicitly requested.
- Folding a correction into this PR's own unpublished commits is pre-approved; rewriting published history still requires explicit permission.

## 8. Failure handling

When a command or test fails:

1. read the actual error;
2. identify the most likely cause;
3. make one targeted correction;
4. rerun only the relevant operation.

- Do not retry the same failing command unchanged.
- Do not escalate one local failure into a repository-wide investigation without evidence.
- Separate environment problems, pre-existing failures, and task-introduced regressions.
- If blocked, report the exact blocker, evidence, and completed partial work without pretending success.

## 9. Stop conditions

Stop when:

- the requested behavior or artifact is complete;
- directly affected behavior has proportional validation;
- the final diff contains only intended changes;
- no known material issue remains within task scope.

Once done, do not run broader checks for reassurance, compute extra hashes, add unrelated tests or documentation, refactor working code, pursue hypothetical edge cases indefinitely, or perform bonus cleanup.

For a small task, prefer one focused discovery pass, one coherent edit pass, one targeted validation pass, and one diff review. Exceed this only when evidence requires it.

## 10. Completion report

Keep the final report concise:

- what changed;
- key implementation decision, when relevant;
- validation performed and result;
- material caveat or unvalidated area;
- commit information, when requested.

Do not include long command transcripts, repeated status summaries, or forensic evidence unless requested.

## 11. Workflow responsibility boundary

- Shell owns operational orchestration: user-facing entrypoints, environment
  handling, executable discovery, process launch, file descriptors, signals,
  traps, cleanup, temporary directories, and sequencing calls into Python.
- Python owns reusable semantics: parsing, scientific validation, identities,
  provenance, artifacts, lineage, locking decisions, reuse, plans, dependency
  rules, and structured results.
- Use stable exit codes and machine-readable JSON between the layers. Do not
  duplicate ORCA validity, convergence, geometry identity, or reuse decisions
  in shell.
- Process health is advisory operational evidence and never scientific success
  or an automatic termination policy.

## 12. Continuous integration

The minimum Python version is 3.11. Keep the qualification matrix limited to
Python 3.11 on Ubuntu 24.04 x64 and Python 3.14 on macOS 15 Apple Silicon;
Python 3.12–3.13 are not qualified. Do not add Python 3.10 compatibility shims.

Keep CI lean and proportional to project risk. Prioritize meaningful
correctness checks, fast feedback, and minimal maintenance. Do not introduce
additional workflows, extensive matrices, or expensive validation without a
demonstrated need.

## Test verification policy

The full test suite is CI's job. Local runs verify the change; they do not
repeat CI.

### Where tests run
- **Qualified combinations** are those listed in README "Tested support"
  (currently Python 3.11 on Ubuntu 24.04 x64, run on every PR, and Python 3.14
  on macOS 15 Apple Silicon, run on main after merge and available as a manual
  workflow run on a branch). Only these are merge gates.
- **Locally**, run the tests related to the change on the local qualified
  interpreter. Run the full local suite only when asked, or when the change
  touches cross-cutting runtime code (Jobs runtime, process groups, the nodes
  endpoint, transport or identity), and never while a calculation on that
  machine is running; use the manual macOS CI run instead.
- **macOS-specific code** (process groups, mounts and volumes, `ioreg`,
  `sysctl`, TCC-protected paths, ACLs and extended attributes, sleep
  assertions) is exercised by PR CI only on Ubuntu. Before merging, trigger the
  manual macOS CI run on the branch, and always wait for and report the
  post-merge main CI result.
- Unqualified combinations (for example macOS with the minimum Python) are not
  merge gates. Report failures there as findings; do not block on them or fix
  them silently.

### How to run
- Choose targeted local tests from the changed modules' tests, adding their
  direct importers where these are few, and tests of changed shell, subprocess
  or other non-import entry points. Broad coverage comes from CI; do not expand
  local selection through transitive reverse imports.
- A running calculation on the machine takes priority. While one is running,
  keep local tests sequential at low priority and total local test time to
  about 10 minutes or less per interpreter; leave broader coverage to CI.
- Never run two full test suites at the same time on one machine. Run
  lifecycle, profile and other timing-sensitive tests one test process at a
  time.
- Each worktree uses its own environment installed from that worktree, so
  comparisons between commits test the intended code.
- Ordinary CI test methods have a 300 s native watchdog; a positive finite
  `CMW_TEST_TIMEOUT_SECONDS` overrides the budget for test fixtures. The fault
  sweep lifecycle methods retain their stricter row watchdog; manifest checks
  use the ordinary guard. The method budget covers setUp, the body, tearDown
  and instance cleanups, not module/class fixtures. A native timeout prints the active
  test and stack, aborts only that isolated module process, and may bypass its
  cleanup; partial counts are incomplete, never a passing suite. The external
  reporter lists registered fixture PIDs and their recorded v2 identities from
  a private ledger; it does not claim to discover unregistered descendants or
  signal other processes. Audit those identities after local runs and clean up
  survivors only after re-verifying ownership. Intentional indefinite `UNVERIFIABLE` holds mean a
  stale mock of an old ownership API can now hang instead of producing a quick
  failure: audit the mock against the actual caller before changing assertions.
- The ordinary suite runs the representative ownership-fault slice. The full,
  unchanged matrix runs in a dedicated job in the same workflow, on each
  qualified platform. Its row-union check requires exactly every unfiltered
  baseline read occurrence times all fault types, with no missing or duplicate
  rows. Report unittest methods and injected rows separately.
- Pushing and opening a PR may proceed while local targeted diagnosis runs;
  merging waits for both.
- Before reporting a PR as ready, read every review comment, including
  automated reviews. Address or explicitly answer each comment.

### Triage before calling a failure real
1. **Load:** rerun the failing test alone. If it passes, it is load-induced.
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
