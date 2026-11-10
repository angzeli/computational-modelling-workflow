# Agent policy consolidation (P4/P9)

Applied to main `c2cde9f`; the audit's line numbers describe `6ffbb8e`.
PR #48 had already consolidated CI scheduling, and PR #45 had corrected LOAD
classification. This change preserves both rather than restoring older rules.
“Global” below means the agent's global working agreement (for Codex,
`~/.codex/AGENTS.md`); those files are not edited by this PR.

## Removed or merged rules

| Former rule(s) | Disposition and current home |
|---|---|
| Universal Codex title | Replaced with repository-specific, agent-neutral “CMW agent rules”. |
| Objective, default loop, instruction precedence | Removed duplication; global Objective. |
| §1: identify goal/constraints/completion, plan proportionally, avoid adjacent scope, clarify blockers, finish efficiently | Removed duplication; global §1. Repository issue/scope limits remain in AGENTS §6. |
| §2: narrow symbol/path discovery, avoid full repository modelling, exclude generated/unrelated data, bound output, reuse evidence, avoid repeated commands/narration, relevance check before extra actions | Removed duplication; global §2. |
| §3: minimal coherent change, reuse helpers, preserve interfaces/behaviour, repository style, avoid unrelated edits, justify abstractions/fallbacks, protect fixtures/generated files, intent-only comments | Removed duplication; global §3. CMW routing and owner approval for new mechanisms remain in AGENTS §§2/6. |
| §4: proportional text/isolated/multi-module/cross-cutting checks, no default full suite, distinct checks, no unchanged reruns, meaningful tests, no unrelated fixes, honest limitations | Removed duplication; global §4. Calculation priority and CMW CI gates remain explicit in AGENTS §§1/5. |
| §5: no routine broad hashes; prefer diff/status/inspection; hash only for identity/integrity and only relevant data | Removed duplication; global §5. |
| §6: existing toolchain, bounded commands, necessary dependency/network changes only, no needless persistent processes, safe temporary cleanup | Removed duplication; global §6. |
| §6: one process snapshot/no repeated polling | Global §6 covers ordinary inspection. AGENTS §4 explicitly permits bounded waiting for requested tests/CI, resolving the conflict with post-merge reporting. |
| §7: preserve user work; no destructive reset/clean; inspect state; stage only intended authorised scope | Removed duplication; global §7. |
| §7: explicit permission to push/open PR versus permission during diagnosis | Merged into AGENTS §4: authorised work permits its commit/push/PR; merge and deployment remain separately authorised. Published rewrites still need explicit permission. |
| Unpublished-commit folding | Retained in AGENTS §4, scoped to the task's own unpublished commits. |
| §8: inspect error, targeted correction/recheck; avoid blind retries and broad investigations; distinguish failure classes; report blockers | Removed duplication; global §8. Repository-specific provisional LOAD/base comparison/newly-gated exception remains in AGENTS §5 and scripts/ci/README.md. |
| §9: stop after intended outcome, proportional validation and scoped diff; no bonus cleanup or extra reassurance checks | Removed duplication; global §9. |
| §10: concise outcome/decision/validation/caveat/commit report; no transcripts | Removed duplication; global §10. Templates supply repository-specific short fields. |
| Reuse-first paragraphs | Condensed into AGENTS §2: map → linked contract → entry point, usage/development distinction, existing implementation, scientific permission, drift/map updates. |
| D32 production/sandbox section | Retained verbatim, including its heading and specification link. |
| Shell/Python responsibility bullets | Condensed into AGENTS §3 without changing responsibility, JSON/exit-code boundary or scientific-evidence rules. |
| Repeated qualification/platform details | README remains authoritative; minimum Python and prohibition on 3.10 shims remain one line in AGENTS §5. |
| Fast/merge/full/path scheduling and path lists | AGENTS §5 links the existing tier contract; scripts/ci/README.md links the single paths.json source. No workflow or trigger changed. |
| Deployment candidate full nightly/main gate | Retained explicitly in AGENTS §5 for the exact commit. |
| Local selection, worktree environments, low priority, ten-minute budget, no full local suite during calculations, no concurrent full suites | Merged into AGENTS §§1/5; operational explanation in scripts/ci/README.md. |
| 300-second watchdog, native timeout scope, fixture ledger, stale mocks, partial counts | Implementation explanation moved to scripts/ci/README.md; no watchdog code changed. |
| Representative/full matrix, exact union, separate method/row counts | Implementation explanation in scripts/ci/README.md; short reporting obligation retained in AGENTS §5. |
| Review all comments; wait before merge; report post-merge CI | Merged into AGENTS §4. |
| Provisional LOAD, recurrence investigation, base comparison, introduced-only gate and new-matrix exception | Condensed in AGENTS §5; procedure in scripts/ci/README.md. PR #45 semantics preserved. |
| Inline study-lifecycle/table-restoration flake list | Deleted as stale. Only tests/KNOWN_FLAKY lists current exact failures with open issues. #43 is closed; #47 remains provisional, not proven causal LOAD. |
| Never change tests merely to pass; never bypass protection; never count skips/incomplete as passes; require counts | Merged into AGENTS §5 and scripts/ci/README.md. |
| Long issue/PR narratives | Fixed template fields and ≤600-character summaries; evidence in links or details. No body-length CI mechanism added. |

## Added scope rules

AGENTS §6 reproduces audit §8.3 verbatim (normal Markdown line wrapping), followed
by the owner's exact minimal-fix/new-mechanism sentence. First-occurrence flakes
are reported in an existing flake issue or to the owner; a new issue requires a
second occurrence. Feature/refactor/post-cutover proposals require the owner's
decision. The existing #47 record is retained, not retroactively deleted.
