# Universal Codex Working Agreement

## Objective

Complete the requested work correctly with the smallest sufficient scope, context, command count, and validation cost.

Default loop:

**locate -> understand -> edit -> targeted validation -> inspect diff -> stop**

These are defaults. More specific user, repository, directory, or task instructions take precedence.

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