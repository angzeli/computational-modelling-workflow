# Bounded VASP result evidence

`cmw.periodic.vasp.results.inspect_result(directory)` reads existing output without
launching an engine, opening Jobs state or writing files. Evidence extraction,
caller-selected acceptance, and artifact finalization are separate operations.
No overall scientific acceptance is emitted without an explicit policy.

## Native dialect and modes

The supported native dialect is VASP **6.6.1** ordinary self-consistent static
calculations (`NSW=0`, `IBRION=-1`) and fixed-cell ionic relaxation (`ISIF=2`,
`IBRION=1/2/3`). Non-spin-polarized and collinear settings (`ISPIN=1/2`), IALGO
38/48 and DAV/RMM iteration tables are covered. The private read-only development
sample established the DAV path in 6.6.1; RMM and collinear variants have synthetic
coverage. Version banners alone never establish general 5.x/6.x support.

Variable-cell relaxation, MD, hybrid/meta-GGA, SOC/noncollinear, response/band,
magnetic constraints, external-force/field and machine-learned force-field modes
remain outside acceptance. Unsupported files can yield diagnostic facts. General
restart, WAVECAR/CHGCAR validity, model accuracy and energy comparability are
unassessed. This is external-runner integration, not a native VASP launcher.

## Evidence and correspondence

OUTCAR iteration boundaries establish chronological electronic evaluations;
OSZICAR (or explicitly selected stdout) supplies DAV/RMM rows. Correspondence
requires matching segment counts, native labels, every printed electronic
energy, final evaluated free energy and sigma-to-zero energy. It is not based on
co-location or blindly pairing lists. Multiple OUTCAR banners require an explicit
zero-based segment selection. An earlier footer cannot complete a later segment.
Earlier concatenated segments remain diagnostic for endpoint finalization.

Electronic-step energy and final `FREE ENERGIE OF THE ION-ELECTRON SYSTEM`
energy are distinct. Free energy, energy without entropy and sigma-to-zero energy
retain names, eV units and evaluation/source references. Native convergence,
reconstructed dE **and** d eps comparisons, iteration budget, and history coverage
remain separate. EDIFF=0 is fixed-count behavior. Reaching NELM can coincide with
convergence; fewer iterations do not establish it. Missing rms(c) is preserved.
An empty electronic table is unavailable evidence, not an observed contradiction;
numeric acceptance remains unknown. A nonempty conflicting table is still refused.

Printed numbers retain their tokens and half-last-place bounds. A threshold
straddled by rounding stays unknown. No scientific tolerance is increased. See
[VASP EDIFF](https://vasp.at/wiki/EDIFF), the
[official clarification of both energy changes](https://alpha.vasp.at/forum/viewtopic.php?t=19143),
[OSZICAR](https://vasp.at/wiki/OSZICAR), and
[EDIFFG](https://vasp.at/wiki/EDIFFG). Positive EDIFFG is energy-based; negative is
force-based; zero is step-budget behavior. Energy-based relaxation acceptance is
not implemented. Native optimizer stopping is separate from force magnitude.
Trial evaluations and repeated labels are never called accepted optimizer steps.

A complete native timing footer, fatal/stop indicators, and an independently
recorded OS exit code are separate facts. No exit code is invented from a footer.
A present STOPCAR cannot establish when it acted; see the
[STOPCAR contract](https://vasp.at/wiki/STOPCAR).

## Periodic endpoint and constraints

The final complete geometry must belong to its force and evaluated-energy block.
POSCAR/CONTCAR reuse the same parser as input checking and preparation, including
optional exact matching elemental row annotations and blank-separated all-zero
Cartesian velocity tails with exactly one triple per atom. The zeros must be
finite and exact in decimal representation; a tiny nonzero value is not zero.
The source text, cell, atom order and selective-dynamics flags are preserved.
Other suffixes, velocities and predictor/corrector sections remain unsupported;
this syntax coverage adds no MD or restart support.
The native OUTCAR species/counts determine the evaluated species identity.

Comparisons preserve cell basis and atom order. ASE's general minimum-image
calculation handles skew periodic cells; there is no sorting, rotation, symmetry
matching or standardization. Default representation tolerances are 2e-5 Angstrom
for positions and 2e-6 Angstrom for cell components, reflecting observed printed
precision. A policy may require a stricter comparison when the native printed
resolution supports it. Static endpoints are compared with POSCAR; CONTCAR is
not mandatory, and the `ionic.relaxation` check is `NOT_APPLICABLE`.
Relaxations require the evaluated endpoint to match CONTCAR, with the fixed cell
and whole-atom constraints retained.
Missing POSCAR or CONTCAR leaves their constraint comparison unknown. Only an
observed disagreement is reported as a mismatch.

Force summaries retain raw vectors and all/free/fixed maxima, counts and zero-based
indices. Fixed-atom forces are not silently zeroed. An empty free set does not
prove relaxation. Partial-direction flags remain visible but their force
projection is unsupported: selective flags refer to direct-lattice directions,
including when coordinates are Cartesian. See
[POSCAR](https://vasp.at/wiki/index.php/POSCAR) and
[ISIF](https://vasp.at/wiki/ISIF).

Current POSCAR flags alone are not verified historical execution inputs. They are
reported as current-input-derived until an explicit binding is validated.
Matching native initial geometry and CONTCAR flags corroborates that binding;
it does not turn a retrospective declaration into a pre-run receipt.

## Source snapshots

OUTCAR and OSZICAR are streamed. Exact consumed bytes are hashed during parsing;
line/byte ranges accompany assertions. Device/inode, size and timestamp evidence
records growth, truncation, replacement and incomplete bounded reads. Numerical
summaries and endpoint geometry are retained, not eigenvalue/charge-grid text.
Non-finite source or derived values become explicit findings and null values,
never nonstandard JSON numbers. No whole-directory hashing is performed.

Snapshots are sequential observations, not an atomic filesystem snapshot. A quiet
file is not proof of completed execution. Finalization revalidates exact source
identities; arbitrary external edits can still race that final check. Reuse must
recheck the recorded sources, policy and bindings. Records describe snapshots,
not permanent certificates for paths.

## Explicit acceptance policies

A schema-1 policy is caller-owned JSON data. Every field below is required;
unknown keys, duplicate keys and non-finite values are rejected. There is no
implicit policy. `PASS` means accepted under that named policy and its scope;
`FAIL`, `UNKNOWN` and `UNSUPPORTED` remain distinct outcomes.

This complete **synthetic static** example requests only the named evaluated
energy; its tolerances are representation comparisons, not DFT accuracy targets:

```json
{
  "schema_version": 1,
  "name": "synthetic-static",
  "calculation": "static",
  "electronic": {"history": "all", "basis": "native-and-numeric", "criterion": "run-ediff"},
  "force": null,
  "endpoint": {"position_tolerance_angstrom": 0.00002, "cell_tolerance_angstrom": 0.000002},
  "require_execution": true,
  "require_input_binding": true,
  "artifact_roles": ["energy:sigma_to_zero"]
}
```

This complete **synthetic constrained relaxation** example assesses the maximum
force norm on free atoms against the magnitude of the observed negative EDIFFG:

```json
{
  "schema_version": 1,
  "name": "synthetic-constrained-relaxation",
  "calculation": "fixed-cell-relaxation",
  "electronic": {"history": "all", "basis": "native-and-numeric", "criterion": "run-ediff"},
  "force": {"criterion": "run-ediffg", "scope": "free"},
  "endpoint": {"position_tolerance_angstrom": 0.00002, "cell_tolerance_angstrom": 0.000002},
  "require_execution": true,
  "require_input_binding": true,
  "artifact_roles": ["periodic-structure"]
}
```

`electronic.history` accepts `all` or `final`; `basis` accepts `native`, `numeric`
or `native-and-numeric`. These choices change assessment, not parser facts.
`criterion` must be `run-ediff`. A limited read never establishes complete history.
Endpoint tolerances must be positive and no larger than the parser tolerances.
An endpoint comparison is `UNKNOWN` if its requested position tolerance is below
`sqrt(3)` times the native position-component half-last-place bound, or its cell
tolerance is below the native cell-component half-last-place bound, even when
the printed values numerically match. Printed precision cannot establish a
stricter match.

For relaxation, `force.scope` is explicitly `all` or `free`. An explicit threshold
instead uses `{"criterion":"explicit","scope":"free","threshold_ev_per_angstrom":0.02}`;
that number is an illustrative caller choice, not a recommended default. Both
force-policy forms still require supported negative-EDIFFG relaxation, native
optimizer stopping, a nonempty free set and coherent endpoint/constraint evidence.
Rounding bounds crossing a threshold produce `UNKNOWN`.

Static policies permit `energy:free_energy`, `energy:without_entropy` and
`energy:sigma_to_zero`. Fixed-cell relaxation can additionally request
`periodic-structure`. Roles must be unique and nonempty. A finalized constrained
structure retains its flags and is not certified as unconstrained relaxation.
No wavefunction, charge-density, interface, general restart or minimum-character
artifact is produced.

For diagnostic inspection, the two provenance requirements may explicitly be
false. Such policy acceptance cannot bypass finalization's mandatory execution
and input-binding checks. Whole-atom constrained relaxation always needs input
binding, even under a policy with `require_input_binding: false`. Inspecting
current flags without the explicit binding below therefore leaves strict
acceptance unestablished.

## Source hierarchy and retrospective binding

The evidence layers retain separate meanings:

| Layer | What it establishes |
| --- | --- |
| Selected OUTCAR and matched OSZICAR/stdout | Native settings, chronological evaluations, convergence indicators, evaluated energies/forces, geometry and termination evidence within the supported dialect. |
| Current POSCAR/INCAR and optional CONTCAR | Exact current bytes and parsed settings/geometry/constraints. Native initial geometry and endpoint comparisons are corroboration, not an execution receipt. |
| Optional completed VASP preparation record | Exact recorded preparation and POSCAR identity; prepared/current INCAR differences. Potential payload and k-point comparability remain unassessed by result finalization. |
| Explicit saved Jobs snapshot and native receipt | Selected operational invocation, working directory, command, timestamps, terminal state, exit and signal evidence. Jobs does not snapshot scientific inputs/outputs. |
| Optional explicit external-runner record | Connects a recognized wrapper input directory to its original output directory, with staged/effective four-file identities and lifecycle corroboration. Native Jobs completion remains mandatory. |
| Finalization specification | A retrospective caller declaration associating those exact observations, selected segment, policy and intended artifacts. It is not a pre-run plan or authenticated input receipt. |

The execution adapter accepts an explicitly supplied native **Jobs schema-2
snapshot** and its original `state_directory/attempts/<attempt UUID>/payload-exit.json`.
It requires one exact positive numeric Job ID and 32-character lowercase attempt
UUID, matching run directory and attempt log paths, `Done`, zero exit in both
records, explicit absent signal/cancellation, and ordered finite timestamps.
A copied receipt at an unrelated path is insufficient. No Jobs store is opened
or mutated. The default contract requires the Jobs working directory to equal
the inspected output directory. The optional bounded runner association below
handles an explicitly recorded wrapper with a separate payload directory.
Saved local records remain caller-owned evidence, not tamper-proof authentication.

The full specification shape is illustrated below. Angle-bracket values are
**non-runnable placeholders**: replace every identity with the actual lowercase
SHA-256 from inspection or exact source bytes, and supply existing native Jobs
evidence. Do not manufacture a completion receipt for a real run.

```json
{
  "schema_version": 1,
  "intent": "retrospective",
  "producing_calculation": "caller-owned-calculation-identity",
  "snapshot_id": "<native inspection snapshot_id>",
  "segment_id": "<selected_segment.segment_id>",
  "policy_id": "<canonical policy content SHA-256>",
  "source_identities": {
    "OUTCAR": "<exact source SHA-256>",
    "OSZICAR": "<exact source SHA-256>",
    "POSCAR": "<exact source SHA-256>",
    "CONTCAR": "<exact source SHA-256>",
    "INCAR": "<exact source SHA-256>"
  },
  "execution": {
    "snapshot": "saved-jobs-snapshot.json",
    "receipt": "/actual/state/attempts/<32-hex-attempt>/payload-exit.json",
    "job_id": 7,
    "attempt_id": "<32-hex-attempt>",
    "source_identities": {
      "jobs_snapshot": "<exact snapshot-file SHA-256>",
      "jobs_receipt": "<exact receipt-file SHA-256>"
    }
  },
  "artifact_roles": ["energy:sigma_to_zero"],
  "expected_runtime_overlay": {"NCORE": 2},
  "preparation_record": {"path": "scratch/preparation.json", "sha256": "<exact record SHA-256>"},
  "parents": [
    {"record": "scratch/parent/finalization.json", "sha256": "<exact record SHA-256>",
     "artifact_id": "<finalized periodic structure artifact ID>", "relationship": "input-structure"}
  ]
}
```

At the top level, only `parents`, `expected_runtime_overlay` and
`preparation_record` are optional.
Omit them when unused. `source_identities` must exactly cover the observed native
source roles: omit absent CONTCAR for a static run; use `stdout` instead of
`OSZICAR` when explicitly selected. It does not include the policy, specification
or Jobs records. Specification and policy artifact-role sets must agree.
Relative execution, preparation and parent paths resolve from the specification's
directory; the original specification is retained separately from resolved paths.

### Optional external-runner association

#### Check a managed runner interface

Before configuring this specific external-port handoff, inspect the selected
source checkout and the complete intended arguments:

```sh
cmw vasp check-runner --runner /path/to/port/scripts/run-vasp.sh \
  --project-root /path/to/project --json -- \
  --input /path/to/prepared-inputs --output /path/to/new-output \
  --binary std --ranks 2 --ncore 1 --kpar 1 --mpi-mode native \
  --restart none --timeout 600 --stop-before 0 --managed-foreground
```

Replace the paths with absolute canonical paths. The input directory must exist;
the output path must not exist. This check reads bounded source snapshots and
Git identity metadata. It does not import, source or execute the runner, even for
help; it does not inspect input contents, stage files or access Jobs state.
Only the recognized shell delegation and straight-line literal argument parser
are assessed. Other wrapper/parser forms are UNKNOWN, without a fallback probe.

The JSON and readable summary separate argument compatibility, managed lifecycle
qualification and result-binding qualification. Exit 0 means the declared
argument interface matches; both qualifications remain `NOT_ESTABLISHED`.
Exit 2 means incompatible or unknown. An absent `--managed-foreground` is reported
explicitly; it is never removed or mapped to a standalone flag. A flag's help
text identifies only a declared contract, not implementation of that contract.

The source combination records available commits, relevant source dirty state,
bounded source hashes, CMW module/distribution origin and the optional project
identity. Whole-worktree dirty state and untracked files outside the known runner
source set are not assessed. Relevant bytes are compared without Git filters,
hooks or worktree diff commands that could execute repository configuration.
Dirty support is a working-tree observation, not a committed compatibility result.
Installed wheels can have no Git commit; package versions, editable paths and
locators alone do not establish equivalence to another source tree. Preserve the
checker output alongside the exact revisions and test evidence for a qualification;
there is no permanent commit allowlist or automatic qualification of descendants.

Jobs resource declarations do not prove actual ranks or threads. The selected
route checks explicit ranks/NCORE/KPAR arguments; it has no `--threads` argument.
Review thread environment propagation separately. Effective INCAR overlays and
staged input identities are verified later by result binding, not by this check.
The checker is an explicit VASP operation, never a generic Jobs admission rule or
controller-tick probe.

For runtime qualification, retain evidence for the exact CMW/port combination.
Jobs owns its default process group; explicit owned-session mode can additionally
own trusted same-session subgroups. It does not adopt new-session escapes or
external observations. The runner must preserve the agreed foreground/session
contract and cancellation/drainage ownership. CMW's benign group/session tests
exercise those CMW contracts, not actual OpenMPI behavior or standalone port cleanup.

Portable `test_handoff_contract` regressions prepare invented static/fixed-cell
inputs, execute a benign protocol fixture through isolated Jobs, retain its native
completion receipt, and finalize/reverify through the common core. Execution
failure and failed scientific policy remain refusals. These tests qualify CMW's
consumer contract; they do not qualify an incompatible committed external port.
ZIS scientific analysis remains project-owned, and a ZIS consumer of finalized CMW
artifacts is not implemented by this integration.

#### Bind the original runner output

For the observed external `run-vasp.sh` integration, `execution` additionally
accepts the optional `runner_record` member below (the hash is a placeholder):

```json
{
  "runner_record": {
    "dialect": "vasp-run-metadata-text-v1",
    "path": "/actual/original/run/RUN_METADATA.txt",
    "sha256": "<exact runner-record SHA-256>"
  }
}
```

This dialect name versions CMW's interpretation of an unversioned text format;
it does not claim the external file carries a native schema version. There is no
automatic record discovery. The original Jobs snapshot and original receipt are
still required; `execution.source_identities` still contains exactly those two
Jobs identities. The additional runner record and runtime inputs enter the
required source closure and artifact identity through the verified association.

The supported invocation explicitly supplies `--input`, `--output`, `--binary std`,
`--ranks`, `--ncore`, `--kpar`, `--mpi-mode`, `--restart none`, `--timeout`,
`--stop-before` and `--managed-foreground`. Counts must satisfy the external
runner's 1–8 rank range and decomposition rules. Both native and synthetic MPI
topology modes are recognized; the latter names a topology setting, not fake
scientific execution. Unknown/repeated options, restart modes and other wrappers
are unsupported. Paths must remain absolute and canonical: Jobs cwd equals the
input argument, and the inspected directory equals the output argument.

Only the original `<output>/RUN_METADATA.txt` is accepted, with one ordered
STAGING/RUNNING/FINISHED lifecycle, successful child/launcher status, no stop or
timeout, and timestamps within the selected Jobs attempt. The record is bounded
to 64 KiB and each input to 64 MiB. All four staged input hashes are required;
runtime INCAR must match the effective INCAR hash, and other runtime inputs must
match staged hashes and sizes. POSCAR/INCAR must also match the native inspection
observations. Symlinks, incomplete reads, malformed or duplicate identities,
copied metadata and relocated output paths refuse association. No input payload
text is retained in the record.

Runner metadata never supplies scientific convergence. This association does not
authenticate historical runner code, dirty checkout state or local records, and
does not validate potential choice, k-point adequacy or restart files. No launcher,
relocation receipt or archive adoption capability is introduced.

### Native source and input binding

The policy ID is `cmw.core.provenance.stable_hash(policy)`, identifying canonical
JSON content. Raw policy-file SHA-256 is separately retained, so whitespace edits
can preserve policy meaning while changing the required source observation.
Snapshot IDs identify native source contents and selected segment IDs identify
specific OUTCAR segments. Neither is a pre-existing scientific target identity.

Input binding requires exact declared POSCAR/INCAR hashes, usable parsed inputs,
matching native initial geometry and complete ordered constraint flags. Observed
filesystem/effective-setting differences are refused unless explicitly declared
as positive integer `NCORE` or `KPAR` runtime overlays and the effective value
matches the declaration. No other scientific setting is waived by that field.
Uncaptured effective settings remain labelled unassessed. An optional preparation
record must be completed schema 1 with kind `vasp-input-preparation`, match its
exact declared hash and prepared POSCAR hash, and have coherent prepared/current
INCAR settings with only those explicitly allowed runtime changes.

A parent must name one verified periodic `StructureArtifact` in a completed VASP
finalization record. Its ordered cell/species/geometry and constraint flags must
match the child's bound POSCAR. The parent record and its required sources are
revalidated; missing, changed, mismatched or duplicate parents refuse the whole
bundle. This bounded `input-structure` relationship does not imply arbitrary
wavefunction, restart or cross-method compatibility.

## Commands, output and reuse

```sh
cmw vasp inspect-result RUN_DIRECTORY --json
cmw vasp inspect-result RUN_DIRECTORY --segment 0 --stdout CAPTURED_STDOUT --json
cmw vasp inspect-result RUN_DIRECTORY --max-bytes 1048576 --json
cmw vasp inspect-result RUN_DIRECTORY --policy POLICY.json --json
cmw vasp inspect-result RUN_DIRECTORY --policy POLICY.json --spec SPEC.json --json
cmw vasp finalize-result RUN_DIRECTORY --policy POLICY.json --spec SPEC.json \
  --scratch-root SCRATCH --record-directory case --dry-run --json
cmw vasp finalize-result RUN_DIRECTORY --policy POLICY.json --spec SPEC.json \
  --scratch-root SCRATCH --record-directory case --json
cmw vasp verify-result-record SCRATCH/case/finalization.json --policy POLICY.json --json
```

`inspect-result --spec` requires `--policy`; both are read-only. Inspection and
finalization accept `--segment` and `--stdout`. Only inspection accepts a byte
limit. Finalization accepts optional `--scratch-mount` and requires a new record
directory under an existing explicit Scratch root. Missing Scratch, symlinks,
source overlap and existing destinations are refused. There is no overwrite,
resume, force-success or missing-Scratch fallback.

The strict example policies intentionally return `UNKNOWN` when inspected without
adequate execution/input binding. Supplying `--spec` performs that read-only check;
only a later explicit `finalize-result` may publish. Finalization additionally
requires a `PASS` assessment, successful bound Jobs evidence, valid input binding,
the final selected segment, valid parents and successful **whole-bundle** core
artifact finalization. These gates remain mandatory under a diagnostic policy
that omits provenance requirements. No partial successful artifact bundle is
published. Dry run checks and previews the result without writing anything.

| Operation | Exit semantics |
| --- | --- |
| Inspection without policy | `0`: evidence inspected, not scientific acceptance; `1`: invalid read/parse; `2`: unsupported mode or segment selection required. |
| Inspection with policy | `0`: policy `PASS`; `1`: `FAIL`; `2`: `UNKNOWN` or `UNSUPPORTED`. Invalid requests/policy/specification exceptions also return `2`. |
| Finalization | `0`: successful preview or completed publication; `2`: rejected request, failed eligibility or publication failure. Inspect `publication.state` and structured error details. |
| Record verification | `0`: recorded content/artifacts and required sources verify; `2`: invalid record, changed source, differing requested policy or request error. |

Publication writes only:

```text
SCRATCH/case/
  finalization.json
```

Prepared inputs, native run files and existing records are unchanged. No POSCAR,
CONTCAR, POTCAR, scientific output or licensed payload is copied. No manifest,
marker, symlink or finalization sidecar is written in the input/run directory.

An exclusive new directory holds durable `intent` before source revalidation.
Only completed validation and publication produce `complete`; ordinary failures
retain `failed` or `incomplete` where possible. An abrupt interruption can leave
`intent`, which is not a completed record and is never automatically reclaimed.
Filesystem synchronization is used, but a failed sync cannot promise power-loss
recovery. Arbitrary external edits may still race the final source check.

The completed record contains the existing core artifact bundle, parser/adapter,
full bounded evidence, policy assessment, retrospective binding, exact required
sources and a record content identity. New calls never overwrite it. Before
reuse, `verify-result-record` rechecks record and artifact identities, validation,
all required current source identities and optional requested policy identity.
Moving/replacing a source or changing its bytes invalidates that verification;
source paths and device/inode identity are intentionally part of this gate.
Verification does not turn local records into authenticated certificates or
replace a consumer's required artifact-type and scientific compatibility checks.
There is no automatic dispatch, archive creation, source relocation or retention
cleanup.

## Runnable synthetic checkout demonstration

This development example creates two uniquely owned synthetic cases. The helper
writes invented output and native-shaped Jobs evidence solely for offline tests;
no engine, runner or actual Jobs store is invoked. Use an existing development
installation with dependencies and run from the checkout root:

```sh
demo="$(mktemp -d "${TMPDIR:-/tmp}/cmw-result-demo.XXXXXX")"
demo="$(cd "$demo" && pwd -P)"
PYTHONPATH="$PWD/src:$PWD" .venv/bin/python - "$demo" <<'PY'
from pathlib import Path
import sys
from tests.periodic.vasp.result_case import make_case
root = Path(sys.argv[1])
make_case(root / "static")
make_case(root / "constrained", relaxation=True, constrained=True)
PY
for case in static constrained; do
  PYTHONPATH="$PWD/src" .venv/bin/python -m cmw.cli vasp inspect-result "$demo/$case/run" \
    --policy "$demo/$case/policy.json" --spec "$demo/$case/spec.json" --json
  PYTHONPATH="$PWD/src" .venv/bin/python -m cmw.cli vasp finalize-result "$demo/$case/run" \
    --policy "$demo/$case/policy.json" --spec "$demo/$case/spec.json" \
    --scratch-root "$demo/$case/scratch" --record-directory accepted --dry-run --json
  PYTHONPATH="$PWD/src" .venv/bin/python -m cmw.cli vasp finalize-result "$demo/$case/run" \
    --policy "$demo/$case/policy.json" --spec "$demo/$case/spec.json" \
    --scratch-root "$demo/$case/scratch" --record-directory accepted --json
  PYTHONPATH="$PWD/src" .venv/bin/python -m cmw.cli vasp verify-result-record \
    "$demo/$case/scratch/accepted/finalization.json" --json
done
```

The constrained fixture deliberately gives the fixed atom a larger force than
the free atom. Its explicit free-atom policy can pass while an all-atom policy
fails; the raw force evidence remains identical. The helper and
[installed smoke driver](../../tests/installed_smoke.py) provide synthetic package
acceptance, not evidence of universal native-output coverage.
