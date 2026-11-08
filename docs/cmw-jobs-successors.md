# Explicit successor-calculation planning

`plan-next`, `materialize-next` and `register-next` separate review, preparation
and held registration. Existing `jobs continue` still resumes the same suspended
processes. These commands never release, dispatch, start or recover a calculation.

## Supported intentions and evidence

- `retry`: the reviewed original scientific inputs, in a new operational attempt.
- `geometry`: a complete observed or independently finalized geometry as a new
  initial guess. An observed geometry is not an optimized/accepted structure.
- `next-stage`: a new explicitly selected scientific stage and its reviewed changes.

Every specification binds an enrolled owner node, queue, internal job ID and
immutable attempt UUID. Native terminal receipt, original worker/domain absence
and an unheld attempt lock must establish closure. Active, unstarted and Unknown
parents block. A failed or cancelled producer can supply a complete evaluated
geometry when its native evidence supports that geometry; exit status does not
establish scientific acceptance. The source-sensitive work stays on the owner.

VASP supports the existing ordinary static/fixed-cell reader and preparer. Its
native initial geometry must corroborate the parent POSCAR; for relaxation the
complete CONTCAR must match the last evaluated geometry and Selective dynamics
flags. Explicit segment selection resolves supported concatenated output.
Accepted mode additionally verifies the original finalization and chosen artifact.
No file-age selection, partial CONTCAR or later unevaluated geometry is accepted.
Current constraint corroboration is qualified as such, not invented pre-run proof.

ORCA supports the existing ordinary all-electron H–Kr SP/OPT input dialect and
ORCA 6.1.x output. Observed coordinates require a complete ordered angstrom table
with at least six decimal places and a subsequent finite native single-point
energy in that evaluation. An explicit frame index disambiguates repeat geometry.
Accepted OPT geometry additionally requires the original successful finalizer and
native optimization convergence. Unsupported constraints, ECPs, composite workflow
parents and FREQ successors refuse. The caller's template must already contain
the selected geometry, an explicit stage, charge/multiplicity, NoAutoStart, PAL
and MaxCore matching the selected existing execution profile.

Electronic restart is always `checkpoint: "none"` in this initial scope.
WAVECAR/CHGCAR/GBW presence does not establish compatibility and those files are
not copied. VASP ISTART/ICHARG restart requests refuse. Cross-host replication or
adoption is not supplied by this route; a copied result does not inherit execution
authority. Engine execution and scientific acceptance remain separately qualified.

## Review, then prepare, then optionally register

```sh
cmw jobs --state /absolute/private/state plan-next J23.1 \
  --spec /absolute/owner/successor-spec.json --json
cmw jobs --state /absolute/private/state materialize-next J23.1 \
  --spec /absolute/owner/successor-spec.json --approve-digest REVIEWED_SHA256 --json
cmw jobs --state /absolute/private/state register-next J23.1 \
  --materialization /absolute/owner/records/successor.json \
  --approve-digest REVIEWED_SHA256 --request-id NEW_UUID_HEX --json
```

Replace placeholders with exact returned identities; optional
`--expected-attempt-id UUID_HEX` adds an explicit CLI binding. For a connected owner
use `--node ALIAS --queue QUEUE` instead of `--state`. Paths always refer to that
owner; the client does not open, reinterpret or copy them. Writable connection and
existing owner/runtime guards apply to mutation. Older workers lacking the
`successor_planner` capability return an explicit unsupported result.

Planning is read-only. Its complete JSON includes original parent evidence, source
qualification, semantic before/after values, runtime profile, resources, new
locations and a content-bound digest. Review the complete plan. Materialization
recomputes and compares that exact digest and source bytes, then uses the existing
no-clobber publishers. VASP's canonical directory contains only INCAR, POSCAR,
KPOINTS and POTCAR; its records go to explicit Scratch and a separate new wrapper
working directory remains empty. ORCA uses its existing target/layout/attempt
materializer. Failed publication preserves incomplete evidence and never registers.

If the parent queue or other required parent evidence cannot be read, planning
reports `UNVERIFIABLE` with the reason and exits 75. Retry the same request.
A contradicted closure reports `BLOCKED` and exits 2. Study comparisons keep
the specific reason on an `UNAVAILABLE` row.

Once the engine has published the approved inputs, `successor.json` records
their hashes, the approved plan digest and a `PENDING_VERIFICATION` state.
If the parent recheck is temporarily unavailable, `materialize-next` returns
that state and exits 75. Registration refuses with exit 75. After access
recovers, repeat **the same materialize command, specification, digest and
destination**. The owner locks the publication directory, checks the input
bytes and parent evidence, then atomically changes the record to
`MATERIALIZED`. Concurrent retries observe a single promotion. A changed
parent or altered input leaves a terminal `INCOMPLETE` record for inspection.
A completed materialization from an older CMW version retains its original
registration route.

On a node connection, a retryable evidence refusal uses
`SUCCESSOR_UNVERIFIABLE`; a publication awaiting its final check uses
`SUCCESSOR_PENDING_VERIFICATION`. Both CLI routes return 75. A lost mutation
reply can instead be `OUTCOME_UNKNOWN`; inspect the original destination or
request receipt before retrying. See the [result-verification exit
semantics](periodic/vasp-result-evidence.md#commands-output-and-reuse).

Registration independently checks the approved prepared bytes, original record
location and runtime. It creates one new Hold attempt through Store, preserving
the parent identity and mode in immutable submission metadata. Retry a lost reply
with the **same request ID and parameters**; the durable request journal returns
the original child. A new request ID cannot register the same materialization
again. A subsequent launch still requires separate release/dispatch consent and
rechecks original-location, inputs, runtime and working-directory identity through
the existing admission path. No catalogue relation or scientific PASS is invented.

### Disk full creating the first successor record

If `materialize-next` reports `ENOSPC` while first creating
`successor.json`, check the approved locations. For VASP, the input directory
can already contain INCAR, POSCAR, KPOINTS and POTCAR, Scratch can contain a
complete `preparation.json`, and the wrapper working directory can exist and
be empty. Registration still requires the separate `successor.json`.

Before attempting that first record creation, the owner now saves recovery
evidence in `preparation.json`: the complete reviewed plan and its digest,
the original specification identity, and the input, record and working
directory identities. After restoring space, repeat **the same materialize
command, specification, approved digest and destination**. Under the publication
lock, the owner checks that evidence against the approval, checks the complete
preparation record and parent, and requires exactly the four approved regular
input files with matching bytes. No symlinks, extra or missing files are
accepted. The record directory must contain only `preparation.json`, and the
original working directory must remain empty. Unreadable paths refuse;
they are never treated as missing.

If all checks pass, the owner creates `successor.json` exclusively. A verified
parent produces `MATERIALIZED`; temporarily unavailable parent evidence produces
`PENDING_VERIFICATION` with exit 75, which uses the retry procedure above.
Registration remains a separate request and cannot proceed while verification
is pending. Concurrent retries create one record; the other retry observes it.
A contradicted parent, changed approval, changed directory identity or input
mismatch refuses without creating a record or changing the retained files.

Older publications without this recovery evidence, or a failure while saving
the evidence itself, cannot be adopted. Fresh-destination recovery remains
available: retain the first publication for inspection, copy the original
successor specification to a new path and choose unused
`adapter.output`, `adapter.record_directory` and `runtime.run_directory`
locations. Run `plan-next` against the same parent and attempt, review the
new locations and digest, then run `materialize-next` with that digest.
Use the new record for any subsequent held registration. If the original
`successor.json` exists and says `PENDING_VERIFICATION`, follow the
same-destination retry procedure above instead.

## Caller-owned schema 1

Required top-level fields, with no additional fields:

```json
{
  "schema": 1,
  "parent": {"node_id": "OWNER_UUID_HEX", "queue_id": "QUEUE_UUID_HEX",
             "job_id": 23, "attempt_id": "PARENT_UUID_HEX"},
  "mode": "geometry", "engine": "VASP", "name": "Reviewed new initial guess",
  "resources": {"cpus": 8, "memory_gib": 12, "mpi_ranks": 8, "threads_per_rank": 1},
  "runtime": {}, "adapter": {}, "approved_changes": [], "checkpoint": "none"
}
```

This is a shape example, not runnable scientific settings. Fill `runtime` and
`adapter` below with reviewed values. `approved_changes` declares the allowed
semantic categories; an unlisted difference refuses. The final exact values are
approved by digest, not category alone. Categories are `geometry/cell`,
`atom/species order`, `constraints`, `k sampling`, `potentials`,
`Hamiltonian/spin/charge`, `solver/convergence controls`, `resources`,
`checkpoint semantics`, `title_comment_format`, `missing_evidence` and
`scientific_stage`. VASP reuses `compare_inputs`; LREAL, ALGO and MAGMOM retain
scientific/numerical categories. Missing baseline runtime evidence stays explicit.
Retry refuses scientific changes. Changed order/species currently refuses without
an existing mapping contract; supported cell/constraint changes require an explicit
adapter allowance plus semantic approval. No parameters are chosen automatically.

VASP `runtime` has exactly `node_config` (absolute enrolled configuration root),
`profile` (VASP profile name), `run_directory` (new absolute wrapper directory),
and `vasp` with explicit `ncore`, `kpar`, `mpi_mode`, `timeout`, `stop_before`.
The preparation spec must declare the same CPUs/ranks/threads and NCORE/KPAR
overlay. The pinned runner receives that exact overlay, restart none and
managed-foreground mode. This bounded route supports one thread per rank and
pins OMP_NUM_THREADS=1; multi-threaded rank contracts remain unsupported.

VASP `adapter` requires `directory` (the parent's declared native output),
`source_mode` (`input`, `observed` or `accepted`), `preparation_spec` (the existing
VASP preparation JSON), `output` (new canonical four-input directory),
`scratch_root` and `record_directory` (relative location inside explicit Scratch).
Optional fields are `scratch_mount`, `segment`, `stdout`, `accepted_record`,
`accepted_artifact_id` and `allow_structure_changes` (explicit supported changes).
The preparation spec's structure must be the selected candidate; its physical,
numerical, potential and runtime settings remain caller-owned.

ORCA `runtime` is the existing molecular Jobs runtime contract (executable,
identity, version, qualification and, when required, runtime contract). Its adapter
has exactly `source`, `template_input`, `execution_config`, `execution_profile`
and `output` (new private project root). `source` requires `mode`, registered
`input`, registered `target`, and `geometry`; observed/accepted modes also require
registered `output`, accepted additionally requires the original `result`.
Optional `evaluation` is a positive native frame index. All paths are absolute,
canonical and owner-local. The template and profile are read without rewriting
scientific choices or resource budgets.

Specifications, plans and records are bounded to 128 KiB; source documents use
bounded existing readers (up to 64 MiB per relevant source). Larger unsupported
sources refuse instead of silently truncating. Queue schema and TUI layout,
scientific card dimensions, keyboard navigation and Machine line are unchanged.
