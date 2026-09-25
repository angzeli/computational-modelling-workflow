# Real ZnIn2S4 VASP acceptance evaluation — September 2026

Status: **representative real-case acceptance passed within the documented scope**.

This bounded evaluation compares CMW with independently extracted native evidence
from 15 existing ZnIn2S4 calculation cases. The public note contains aliases,
aggregate observations and software findings. Native outputs, structures,
potential data, production paths and machine-specific records remain private.
The sample tests input-dialect and result-integrity contracts; it does not establish a
surface model's physical suitability or a generally validated VASP parser.

## Baseline and evidence basis

The starting checkout was clean `main` at
`9c6c21278987c8952c8ee7d4a6fbd8cfb7c67c65`, with zero commits ahead of or behind
the fetched origin. Candidate work uses `forensic/real-zis-vasp-acceptance`.
The baseline [Python CI run](https://github.com/angzeli/computational-modelling-workflow/actions/runs/35818632180)
passed its macOS/Python 3.14 and Linux/Python 3.10 jobs. The baseline
[Pages run](https://github.com/angzeli/computational-modelling-workflow/actions/runs/35818632200)
also passed. These are baseline results, not candidate acceptance evidence.

The private baseline was frozen before source changes in
`production-matrix-before.json` and `findings-before-coding.json`.
`native-corpus-independent-15.json` and its Markdown companion contain the
independent extraction. These filenames identify retained local evidence; the
private files are not distributed with CMW.

The two evidence routes were separate:

- The independent helper imported no CMW parser. It counted native electronic
  cycles and iterations, convergence and termination markers, completed energy
  and force blocks, and ordered endpoint comparisons. It retained source
  identities, stable-stat observations and line references privately. It
  recorded observed run settings without selecting an acceptance threshold.
- Baseline CMW inspected the same bounded corpus, checked available four-file
  input bundles, and applied explicit diagnostic and strict policies. Its
  printed-precision checks and general periodic minimum-image comparisons are
  distinct from the independent helper's point-value calculations.

All 15 cases identify VASP 6.6.1 and contain one native banner. The independent
sample establishes DAV coverage. XML was inventoried by existence and size, not
parsed by that helper; the helper also read no potential payload. Local CMW
input checking is a separate route that inspects the supplied four input files.
No native or licensed payload is included in this note or its synthetic tests.

## Independent native observations

The counts below are electronic cycles and electronic iterations, not a count of
accepted optimizer steps. `EDIFF yes/no` counts native convergence/nonconvergence
markers. Force values are rounded summaries in eV/Angstrom. A missing ionic stop
is not a relaxation failure criterion for a static calculation.

| Alias | Case type | Cycles / iterations | EDIFF yes/no | Ionic stop | Normal footer | Maximum all-atom force |
| --- | --- | ---: | ---: | --- | --- | ---: |
| E3 | Nonconverged static | 1 / 60 | 0 / 1 | No | Yes | 3712.420689 |
| E2x | Difficult converged static | 1 / 69 | 1 / 0 | No | Yes | 0.358811 |
| R4L | Short relaxation | 3 / 124 | 3 / 0 | No | Yes | 0.141429 |
| P4L | Geometry continuation with fresh electrons | 16 / 200 | 16 / 0 | Yes | Yes | 0.018183 |
| F4L | Tighter static endpoint audit | 1 / 139 | 1 / 0 | No | Yes | 0.018508 |
| T2L | Thin-slab relaxation control | 33 / 257 | 33 / 0 | Yes | Yes | 0.013555 |
| T4L | All-free relaxation | 14 / 169 | 14 / 0 | Yes | Yes | 0.018025 |
| T6L-failed | Interrupted, electronically unconverged attempt | 1 / 120 | 0 / 1 | No | No | Unavailable |
| T6L-retry | Retry preserved as output-only archive | 16 / 324 | 16 / 0 | Yes | Yes | 0.015962 |
| C4L | Whole-atom constrained relaxation | 28 / 263 | 28 / 0 | Yes | Yes | 0.100972 |
| Bulk | Bulk static | 1 / 15 | 1 / 0 | No | Yes | 0.007795 |
| R1-restart | Interrupted checkpoint-restart fragment | 0 / 0 | 0 / 0 | No | No | Unavailable |
| B1-reconstruction | Long edge relaxation | 100 / 1772 | 100 / 0 | No | Yes | 0.037415 |
| J40-beta100 | Original-location candidate relaxation | 100 / 1500 | 100 / 0 | No | Yes | 0.233503 |
| J41-IIb001 | Original-location candidate relaxation | 9 / 230 | 9 / 0 | Yes | Yes | 0.016850 |

C4L has 14 free and 14 fixed atoms. Its maximum free-atom force is 0.012752,
while its maximum fixed-atom force is 0.100972. An all-atom maximum cannot replace
the declared free-atom criterion, and the constraint interpretation still needs
input binding. T6L-retry lacks the input needed to reconstruct that partition;
its all-atom force observation does not supply missing constraint evidence.

J40-beta100 has converged electronic cycles and a normal footer, but no native
ionic stopping marker and a maximum force above its observed 0.02 criterion.
J41-IIb001 has nine converged cycles, native ionic stopping and a maximum force
below the same observed criterion. Their independently measured endpoint-to-
CONTCAR displacements are approximately 7.5e-6 and 7.1e-6 Angstrom respectively.
Those facts concern native result integrity; execution binding and finalization
are separate checks.

## Frozen CMW results before changes

Both policies required all electronic evaluations, native and numerical
convergence against the observed `EDIFF`, and endpoint agreement within 2e-5
Angstrom for positions and 2e-6 Angstrom for cell components. Relaxations used
the observed force-form `EDIFFG` on free atoms and requested a periodic-structure
artifact; statics requested `energy:sigma_to_zero` and no force criterion.
The strict policy additionally required execution and input binding. No binding
specification was supplied for this baseline matrix.

Diagnostic PASS is acceptance under those diagnostic requirements only. It is
neither a completed artifact record nor permission to finalize or reuse a
result. Whole-atom constrained results require input binding even under the
diagnostic policy. The separate four-input checker status is shown explicitly;
an unsupported checker dialect is not itself evidence of failed science.

| Alias | Four-input checker | Diagnostic policy | Strict policy | Decisive baseline observation |
| --- | --- | --- | --- | --- |
| E3 | UNSUPPORTED | FAIL | FAIL | Native and numerical electronic failure despite normal footer |
| E2x | UNSUPPORTED | PASS | UNKNOWN | Native checks pass; no execution/input binding supplied |
| R4L | UNSUPPORTED | FAIL | FAIL | Force exceeds criterion; no native ionic stop |
| P4L | UNSUPPORTED | PASS | UNKNOWN | Native checks pass; no execution/input binding supplied |
| F4L | UNSUPPORTED | PASS | UNKNOWN | Static endpoint checks pass; no binding supplied |
| T2L | UNSUPPORTED | PASS | UNKNOWN | Native checks pass; no binding supplied |
| T4L | UNSUPPORTED | PASS | UNKNOWN | Native checks pass; no binding supplied |
| T6L-failed | UNSUPPORTED | FAIL | FAIL | Native nonconvergence, incomplete endpoint and table disagreement |
| T6L-retry | INVALID | FAIL | FAIL | Missing inputs; baseline incorrectly calls absent constraint evidence a mismatch |
| C4L | UNSUPPORTED | UNKNOWN | UNKNOWN | Whole-atom constraint interpretation lacks binding |
| Bulk | UNSUPPORTED | PASS | UNKNOWN | Static checks pass; no binding supplied |
| R1-restart | UNSUPPORTED | FAIL | FAIL | No evaluated endpoint; baseline incorrectly calls zero-byte OSZICAR a conflict |
| B1-reconstruction | UNSUPPORTED | FAIL | FAIL | Force exceeds criterion; no native ionic stop |
| J40-beta100 | UNSUPPORTED | FAIL | FAIL | Electronic convergence does not satisfy ionic/force requirements |
| J41-IIb001 | UNSUPPORTED | PASS | UNKNOWN | Native checks pass; execution/input binding still separate |

The frozen diagnostic totals are seven PASS, seven FAIL and one UNKNOWN. The
strict totals are seven FAIL and eight UNKNOWN. No strict PASS or finalized
artifact is implied by this baseline.

## CMW results after the bounded fixes

All 15 native snapshot identities are exactly unchanged from the frozen
baseline. Thirteen complete input bundles now pass the shared checker dialect.
T6L-retry remains INVALID because required inputs are absent. R1-restart remains
UNSUPPORTED because its restart mode is outside the input-checking contract.
Neither change grants scientific acceptance to an incomplete or failed run.

The after matrix again supplies no binding specification. Its strict UNKNOWN
results therefore remain separate from the explicitly bound finalization tests
below. The only policy-status changes are T6L-retry and R1-restart: FAIL becomes
UNKNOWN because absent evidence is no longer reported as an observed conflict.

| Alias | Four-input checker after | Diagnostic after | Strict after |
| --- | --- | --- | --- |
| E3 | VALID | FAIL | FAIL |
| E2x | VALID | PASS | UNKNOWN |
| R4L | VALID | FAIL | FAIL |
| P4L | VALID | PASS | UNKNOWN |
| F4L | VALID | PASS | UNKNOWN |
| T2L | VALID | PASS | UNKNOWN |
| T4L | VALID | PASS | UNKNOWN |
| T6L-failed | VALID | FAIL | FAIL |
| T6L-retry | INVALID | UNKNOWN | UNKNOWN |
| C4L | VALID | UNKNOWN | UNKNOWN |
| Bulk | VALID | PASS | UNKNOWN |
| R1-restart | UNSUPPORTED | UNKNOWN | UNKNOWN |
| B1-reconstruction | VALID | FAIL | FAIL |
| J40-beta100 | VALID | FAIL | FAIL |
| J41-IIb001 | VALID | PASS | UNKNOWN |

After diagnostic totals are seven PASS, five FAIL and three UNKNOWN; strict
totals are five FAIL and ten UNKNOWN. Genuine convergence and force failures
remain failures, and the unbound constrained C4L result remains UNKNOWN.

### Comparison with the independent native extraction

A separate comparison of retained summaries imported no CMW parser and reread
no production payload. For all 15 aliases, OUTCAR identities agree with the
independent extraction, and cycle and iteration counts agree exactly. All 39
available final energy values across the three energy roles agree exactly at
their reported 5e-9 eV half-last-place resolution. Force partition counts and
availability also agree, including the unavailable free/fixed partition for
T6L-retry and absent endpoints for the two incomplete cases.

The 26 available force maxima differ by at most 2.8e-17 eV/Angstrom. The comparison
bound was sqrt(3) times the reported 5e-7 eV/Angstrom force-component
half-last-place, approximately 8.7e-7 eV/Angstrom. No comparison mismatch was
found. These numerical checks corroborate extraction integrity, not scientific
suitability. The detailed comparison proof remains in the private study folder.

## Findings frozen before coding

The ledger deliberately separates defects, coverage gaps, expected boundaries
and project science. The implementation and regression column states the bounded
work to verify; final outcomes belong in the candidate acceptance section below.

| ID / classification | Evidence and expected behavior | Bounded implementation or regression obligation |
| --- | --- | --- |
| F1 — REAL-DATA COVERAGE GAP | E2x, P4L, Bulk and J41-IIb001 expose exact elemental row annotations or blank-separated zero velocity tails. Result inspection already handles these forms, while checking/preparation refuse them. A shared dialect should preserve original bytes and atom order. | Extend existing `parse_poscar`; reuse it from result inspection. Synthetic cases cover annotations, zero tails, their combination, selective flags and repeated blocks; arbitrary suffixes, malformed/nonzero/underflow tails remain refused. Verify exact preparation bytes. |
| F2 — TRUE BUG | T6L-retry has absent POSCAR constraint evidence; R1-restart has an empty OSZICAR. Baseline reports observed mismatches/conflicts where evidence is missing. Missing evidence should be UNKNOWN/unavailable. | Retain an absent constraint comparison and distinguish zero-byte tables. Regress missing/empty inputs alongside genuinely mismatched flags and nonempty contradictory tables, which must remain failures. |
| F3 — REAL-DATA COVERAGE GAP | J40-beta100 and J41-IIb001 have genuine saved Jobs Done/exit-zero facts and native receipts. Their foreground wrapper stages inputs to a separate output directory, so the baseline working-directory equality gate rejects execution binding. | Add only an explicit bounded external-runner record adapter: exact command/input/output relation, staged/effective hashes and terminal/timestamp evidence. Jobs receipts remain mandatory. Regress wrong paths, hashes, attempts, duplicates, truncation, stops, relocation and missing evidence; finalizer/verifier must reuse the same checks. |
| F4 — EXPECTED EXTERNAL-INTEGRATION BOUNDARY | The 11 archived/relocated cases E3 through Bulk in the matrix do not provide exact original-output-location provenance; some also omit inputs. Native scientific facts cannot repair missing source identity. | No general relocation or archive adoption. Inspect retained evidence and refuse finalization until sufficient supported identity evidence exists. |
| F5 — FALSE ALARM / CURRENT CONTRACT CORRECT | E3, R4L, T6L-failed, B1-reconstruction and J40-beta100 exhibit actual SCF failure, incomplete/conflicting histories, missing optimizer stopping or excessive forces. A footer or zero exit does not invalidate those findings. | Preserve separate convergence, termination and suitability verdicts. No permissive acceptance change. |
| F6 — PROJECT-SPECIFIC SCIENCE — NOT CMW | Displacements and possible reconstruction in B1-reconstruction and J40-beta100, and candidate suitability for J41-IIb001, need project interpretation. | Report ordered periodic endpoint and forces. Do not infer chemical suitability, malformed geometry or model validity from displacement magnitude. |
| F7 — REAL-DATA COVERAGE GAP | The 15-case corpus has no real RMM or appended multi-banner sample. Inspection of 42 saved jobs established no completed, unrelocated static case. | Keep those native coverage gaps explicit. Synthetic adversarial cases may exercise segmentation; an energy artifact from a relaxation does not establish native static execution coverage. |

## Execution, science and binding

Native convergence and force evidence answer different questions from a Jobs
completion record. An authentic Done/exit-zero receipt can coexist with failed
ionic acceptance, as J40-beta100 illustrates. Conversely, an archived output can
have convincing convergence evidence without a supported execution/source
identity chain.

The F3 integration corroborates the original staged output while preserving the
recorded Jobs working directory. It does not rewrite receipts, adopt relocated
archives, or trust unbound runner text as scientific acceptance.
A retrospective declaration binds the selected snapshots; it does not create
an authenticated pre-run input receipt. Successful finalization still requires
the shared core artifact checks and a new Scratch-only record, followed by
verification before reuse. See the
[VASP result contract](../periodic/vasp-result-evidence.md).

### Actual original-location finalization

Separate explicit retrospective specifications bound J40-beta100 and J41-IIb001
to their scientific snapshots, saved Jobs completion evidence and supported
external-runner records. Both bindings were valid and both executions eligible.
The retained evidence is summarized in the private `finalization-source/summary.json`.

| Case | Bound policy | Finalization outcome |
| --- | --- | --- |
| J40-beta100 | FAIL | Refused with `FINALIZATION_REFUSED`; no finalization record created |
| J41-IIb001 | PASS | Completed two core artifacts: `periodic-structure` and `energy:sigma_to_zero`; record verification valid |

J41-IIb001 published a new Scratch record directory containing only
`finalization.json`. Initial verification covered 12 distinct required sources. The
energy artifact comes from the accepted relaxation endpoint; it does not close
the missing native static-execution coverage. J40-beta100 demonstrates that
valid operational provenance cannot override failed ionic/force acceptance.

### Installed production workflow and a stale-record observation

The fresh Jobs wheel, imported from its environment's `site-packages` with
`PYTHONPATH` removed and the working directory outside the checkout, reproduced:

| Case | Installed policy result | Finalization / reuse evidence |
| --- | --- | --- |
| E2x | PASS, diagnostic | Read-only inspection; no execution binding or finalization claimed |
| E3 | FAIL | Read-only inspection preserves electronic rejection |
| T6L-retry | UNKNOWN | Read-only inspection preserves missing-evidence boundary |
| J40-beta100 | FAIL, valid binding / eligible execution | Preview and materialization refused; no record |
| J41-IIb001 | PASS, valid binding / eligible execution | Dry run writes nothing; two artifacts published to a second new Scratch record; verification succeeds; collision refused |

J40/J41 were also inspected through the installed text CLI. The J41 installed
artifacts have the same identities as the source-run artifacts. The source
environment independently verified the installed record, so acceptance is not
limited to a single interpreter environment. Each import path was checked;
installed runtime files match all six changed runtime modules byte for byte.

The installed verifier **refused the earlier source-run record**. Its Desktop
specification retained identical bytes, inode, size and modification timestamp,
but its change timestamp (`ctime_ns`) had changed after the original observation.
The cause of that metadata change was not established. This is the existing
strict source-identity contract working as designed, not a content-change claim
or a numerical disagreement. The old record was retained unchanged. The new
installed record captures the current source observation and verified in both
environments. No production file was altered to manufacture this condition.

This additional finding is **FALSE ALARM / CURRENT CONTRACT CORRECT**: source
reverification can reject a same-content file whose recorded filesystem identity
changed. The private `installed-production/summary.json` and
`stale-desktop-spec-observation.json` retain the exact comparison. An earlier
successful verification is not a permanent certificate for a pathname.

### Changes and adversarial coverage

F1 and F2 were demonstrated by failing synthetic regressions before their fixes.
F3 adds an explicit parser for one observed external-runner record dialect;
it neither launches VASP nor invents completion evidence. It retains exact native
Jobs selection, original receipt location, wrapper/output association and
four-file input identities. Artifact identity includes runner context, and
verification repeats the association and checks complete nested source records.
Independent review caught an initially omitted nested-source consistency check;
a synthetic tampering regression now covers it. Top-level source integrity was
already protected, and no production source-integrity bypass was demonstrated.

The new tests add 34 test methods across POSCAR compatibility, missing evidence,
runner association/finalization and continuations. Tests use invented data only.
They cover successful output followed by failed or incomplete continuation,
failed output followed by selected successful continuation, empty repeated
headers, multiple electronic/ionic cycles, final-versus-all history selection,
stale CONTCAR and contradictory table order. No further segmentation defect was
demonstrated. Existing regressions continue to distinguish tiny numeric dE from
formal convergence, NELM behavior, EDIFFG signs, force scope and printed precision.

Changed INCAR/POSCAR/KPOINTS/POTCAR or metadata, missing native receipts, malformed
or duplicate identities, wrong paths/attempts, copied metadata, symlinked inputs,
stops and unsuccessful completion refuse association or reuse. Saved-record
tampering and incomplete source closure also refuse reuse. Existing publication
tests and installed smoke cover no-clobber behavior, failed source revalidation,
failed/incomplete publication and absence of a successful partial artifact bundle.
Mutation/failure injection used synthetic cases; production remained read-only.

## Validation and installed acceptance

The configured repository gate is `unittest`; no pytest or Ruff/lint/format gate
is configured. No new test framework, dependency or CI workflow was introduced.

| Check | Result |
| --- | --- |
| Baseline focused result suite | 116 tests passed before changes |
| Final focused VASP suite, Python 3.14.3 | 267 tests passed in 2.851 s |
| Full deterministic offline suite, Python 3.14.3 | 1,061 tests passed in 439.831 s, isolated test Jobs state and synthetic engines |
| Focused VASP suite against installed Python 3.10.20 wheel | 267 tests passed in 5.647 s |
| Existing artifact acceptance script, Python 3.14.6 | Wheel and source distribution built; core wheel, Jobs wheel, source install and editable install all passed their full installed smoke and dependency checks |
| Additional installed Python 3.10.20 Jobs wheel | Full installed smoke passed outside checkout |
| Fresh installed real production CLI | Five representative cases above; J41 two-artifact publication/reuse and J40 refusal verified |
| Publication hygiene / diff checks | Passed for tracked source and built wheel/source distribution; synthetic fixtures only |

The relevant reproducible commands are:

```sh
python -m unittest discover -s tests/periodic/vasp -t .
CMW_JOBS_STATE="$CHECK_ROOT/unused-state" \
  CMW_TEST_EVIDENCE_DIR="$CHECK_ROOT/lifecycle" \
  python -m unittest discover -s tests
PYTHON_BIN="$(command -v python)" bash scripts/ci/check_artifacts.sh "$NEW_EXTERNAL_DIRECTORY"
```

`CHECK_ROOT` and `NEW_EXTERNAL_DIRECTORY` must identify owned test locations;
the artifact directory must not already exist. The installed production workflow
used the same public commands documented in the
[result contract](../periodic/vasp-result-evidence.md#commands-output-and-reuse),
with actual private source/policy/specification paths. Those locators are retained
in the private study instead of embedding research paths here.

The artifact checks exercise asset loading, CLI text/JSON, optional Jobs/TUI
boundaries, CIF import, VASP preparation and exact four-file bundles, result
inspection/policy/finalization/verification, and synthetic Jobs lifecycle behavior.
The fresh core/wheel/source imports resolve from installed packages; the explicitly
editable case is separately identified. The lower-bound interpreter was installed
only in the owned study directory, without replacing system Python.

All candidate validation here ran on macOS ARM64. Candidate Linux hosted CI was
not run because the branch was not pushed. The verified successful baseline
Linux/Python 3.10 job does not substitute for a candidate Linux run. Existing
NumPy/ASE deprecation and test resource warnings did not fail the suite.

## Source, commits and safety

The initial CMW worktree was clean. Existing unrelated changes in the scientific
project were preserved; its final short status contains the same paths. The
following local commits are chronological:

| Commit | Change |
| --- | --- |
| `384eceac4dc8fd9851d98b24632caf8a5c293939` | Missing evidence versus observed conflicts |
| `3c1ddd1ceb0ac9f7ec6139d1e0a0b1f399776f9d` | Shared lossless native POSCAR syntax |
| `3c943be8457d531a9fe9024361efd0119077c398` | Explicit original-output runner association and complete verification |
| `2790721d8dfd73161feafa6411767b8883b4cdfc` | Continuation adversaries and installed runner acceptance |

The following documentation commit contains this report, capability-map changes
and the updated result contract. Its exact self-containing revision can be read
with `git log -1 --format='%H %s' -- docs/evaluations/real-zis-vasp-acceptance-2026-09.md`;
the task closeout also reports the final HEAD and complete five-commit list.

No real scientific engine was launched, production queue mutated, production
calculation deleted, or real POTCAR/CIF/structure/output payload committed.
Private scientific outputs were read in place; no output directory was copied
or rewritten to obtain acceptance. Hashes, source stats and parsed observations
were recorded only where the integrity contract required them. Before/after
native identities agree for all 15 cases. Analysis writes were confined to the
owned private study directory and new Scratch record directories. Dry runs
published nothing. Nothing was pushed, tagged, released, rebased or force-reset.

## Material limits

- The native sample covers VASP 6.6.1 DAV only. Real RMM, appended multi-segment
  output and completed unrelocated static execution remain unestablished.
- The independent endpoint helper searches 27 neighboring periodic images for
  the observed cells; it is not a general replacement for CMW's ASE comparison.
- The sample does not validate XML parsing, wavefunctions, charge densities,
  magnetic order, potential/mesh comparability, or physical surface suitability.
- Archives missing inputs or exact execution-location evidence remain useful
  for inspection, but cannot be finalized through an invented provenance chain.
- The public note is a summary of private evidence, not a redistributable native
  benchmark. Committed regression inputs must be invented fixtures.
