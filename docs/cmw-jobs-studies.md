# Explicit VASP convergence studies

CMW can plan a caller-defined, bounded study, publish its prepared inputs, register
its cases on Hold, and compare selected results under one reviewed rule. These
are separate operations. None starts a calculation, releases a hold, changes
dispatch, or chooses scientific settings.

## Supported scope and reuse

The first delivery supports static VASP inputs in the existing
[VASP preparation dialect](periodic/vasp-preparation.md): explicit ENCUT values,
explicit regular Gamma or Monkhorst-Pack meshes, and named, byte-pinned reviewed
POSCAR files. It does not generate slabs, vacuum, defects, coordinates, or other
structures. Fixed-cell relaxation, MD, variable-cell, checkpoint reuse, other
engines, adaptive parameter selection, and cross-engine energy comparison are
outside this study contract.

A validated `WorkflowGraph` records independent scientific cases. The existing
standalone VASP preparer, potential selector, input checker, `compare_inputs`, and
split publisher resolve and publish them. VASP does not acquire a generic
`ExecutionRenderer` or `WorkflowPlanMaterializer` adapter through this feature.
The existing Jobs store, durable request journal, catalogue, enrolled runner
profile, admission checks, and owner-node result evidence remain authoritative.
There is no second scheduler.

## Caller-owned manifest

A schema-1 JSON manifest requires these fields. Unknown fields and duplicate JSON
keys are rejected. Paths locate files on the owning node; the manifest and CLI
record paths must be absolute. Source and publication paths inside the manifest
may be relative to the manifest and are resolved before approval. Execution
configuration and run-root paths are explicit absolute owner paths.

| Field | Required declaration |
| --- | --- |
| `schema`, `engine` | `1`, `"VASP"` |
| `study_id` | A caller-chosen lowercase UUID represented by 32 hexadecimal characters; keep it for registration retries |
| `name` | Human study label, at most 160 characters |
| `baseline` | `directory` and `inputs`; `inputs` pins **all four** INCAR/POSCAR/KPOINTS/POTCAR files using `{ "sha256": "…", "size_bytes": N }` |
| `preparation_spec` | An existing schema-1 static VASP preparation specification with explicit scientific settings, selected potential library, and runtime overlay |
| `matrix` | Nonempty `encut`, `kmeshes`, and `structures` lists, as described below |
| `cases` | Optional explicit list of named selections from those lists; otherwise their Cartesian product is used |
| `varied` | Explicit allowed differences: `ENCUT`, `k_sampling`, `geometry`, `cell`, `composition`, `species_order`, `constraints` |
| `scientific_policy` | `{ "path": "…", "sha256": "…" }`, naming exact reviewed static [result-policy](periodic/vasp-result-evidence.md) bytes |
| `metric` | One `{ "name": "free_energy" \| "without_entropy" \| "sigma_to_zero", "unit": "eV" }` |
| `normalization` | `{ "kind": "none" }` or `{ "kind": "per_atom" }`; see comparison restrictions below |
| `comparison_rule` | `{ "kind": "absolute-to-reference", "reference_case": "case name or case_id", "tolerance": positive_finite_number }` |
| `execution` | Exact `node_id`, `queue_id`, `node_config`, enrolled VASP `profile`, `resources`, `vasp`, and `run_root` |
| `output_root` | Existing input-container directory; each case gets a new child directory |
| `scratch_root`, `record_root` | Existing Scratch and record-container directories; `record_root` is strictly inside `scratch_root` |
| `scratch_mount` | Optional explicit mounted-filesystem requirement, inherited from preparation |

The scientific policy must include the selected energy role and pass the existing
static policy validator. CPU/RAM/MPI declarations are separate from the scientific
matrix. `execution.resources` is exactly `cpus`, `memory_gib`, `mpi_ranks`, and
`threads_per_rank`; this initial managed VASP route requires one thread per rank.
`execution.vasp` is the existing runner's explicit `ncore`, `kpar`, `mpi_mode`,
`timeout`, and `stop_before` declaration. The preparation's runtime counts and
NCORE/KPAR overlay must agree with those declarations and the enrolled profile.

For illustration only, a **synthetic** two-cutoff, two-mesh declaration is:

```json
{
  "encut": [300, 400],
  "kmeshes": [
    {"name": "g1", "mode": "Gamma", "mesh": [1, 1, 1], "shift": [0, 0, 0]},
    {"name": "g2", "mode": "Gamma", "mesh": [2, 2, 2], "shift": [0, 0, 0]}
  ],
  "structures": [
    {"name": "reviewed", "path": "reviewed.POSCAR", "sha256": "REVIEWED_FILE_SHA256"}
  ]
}
```

These numbers are test examples, not recommended scientific parameters. Replace
the non-hash placeholder with the reviewed file identity. Explicit `cases` entries
use `{ "name": "reference", "structure": "reviewed", "encut": 400,
"kmesh": "g2" }`. Distinct names may deliberately select identical content;
they remain distinct case identities. Names are safe path components with a
maximum of 96 characters. Generated names are `case-NNN-STRUCTURE-MESH`, numbered from `001` in caller
structure, ENCUT, and mesh order. An explicit reference name resolves to exactly
one immutable case ID before approval; unknown or ambiguous references are refused.

The planner computes the case count **before expansion** and refuses more than
64 cases. Each matrix axis is also bounded to 64 explicit entries. The manifest
and referenced JSON sources have a 128 KiB planning bound; scientific input
sources have a 64 MiB per-file bound. Typed local/remote responses are limited to
256 KiB, so a large structure or detailed matrix can hit the response bound before
64 cases. This is an explicit refusal, not silent truncation or a partial plan.

Baseline identities are never refreshed from current bytes. Every case preserves
exact potential dataset bytes and species block order. Non-varied geometry,
constraints, spin, charge, Hamiltonian, and numerical controls must remain fixed.
Unknown baseline INCAR tags make scientific comparison unassessed and block
materialization. The first delivery cannot vary arbitrary INCAR tags merely by
putting their names in `varied`. A preparation `source_record` is not inherited;
use the explicitly pinned reviewed POSCAR route.

## Plan, publish, then register

With caller-supplied environment variables pointing to reviewed owner files:

```sh
cmw jobs --state "$CMW_STATE" study plan --manifest "$STUDY_MANIFEST" --json
cmw jobs --state "$CMW_STATE" study materialize --manifest "$STUDY_MANIFEST" \
  --approve-digest "$STUDY_DIGEST" --json
cmw jobs --state "$CMW_STATE" study register --record "$STUDY_RECORD" \
  --approve-digest "$STUDY_DIGEST" --request-id "$STUDY_ID" --json
```

`STUDY_DIGEST` is the top-level `plan_digest` of the reviewed owner service result,
not the nested preparation digest. `STUDY_ID` is the manifest's UUID namespace.
`STUDY_RECORD` is `record_root/study.json`, reported by materialization.

Plan reads and validates the entire declaration, enrolled owner/profile,
scientific invariants, and sources. It creates no scientific input directories,
Jobs, locks, or sidecars. Review its case differences and exact digest before
materialization. Source, policy, destination, or runtime changes require a new
reviewed plan.

Materialization re-resolves the approved plan. Each new input directory contains
exactly `INCAR`, `KPOINTS`, `POSCAR`, and `POTCAR`. Full preparation provenance stays
in `record_root/CASE/preparation.json`; the plan response carries a compact
projection of that record. The enrolled runner keeps its working directory at the canonical four-input
bundle. A separate new directory under `run_root` is reserved for its future
`native` output child; both directory identities are bound. The aggregate
`study.json` is separate. Existing destinations are refused.

Publication across cases and locations is not atomic. A failure retains completed
case publications and failed/incomplete per-case evidence; later cases remain
pending. A matching four-file directory does not convert a failed publication
into a completed case. Inspect the explicit partial result. Materialization does
not overwrite or automatically resume a partial publication.

Registration uses a deterministic durable request identity for each study case
and creates new **held** attempts. It records individual receipts in `record_root`.
After a lost response or partial registration, repeat the same record, approved
digest, and study UUID to recover existing attempts and finish eligible cases
without duplicates. It never re-holds or starts a running attempt. Registration
also sets the existing catalogue series/stage labels; these human labels are not
scientific or identity authority. No atomic cross-node registration is claimed.

## Selected result evidence and comparisons

`study compare` consumes an explicit selection document:

```json
{
  "schema": 1,
  "study_id": "MANIFEST_UUID_HEX",
  "plan_digest": "APPROVED_OWNER_PLAN_DIGEST",
  "cases": {
    "CASE_ID_FROM_PLAN": {
      "target": {
        "node_id": "owner-node",
        "queue_id": "owner-queue",
        "job_id": 7,
        "attempt_id": "EXACT_REGISTERED_ATTEMPT_HEX"
      },
      "spec_path": "/owner/reviewed-result-binding.json"
    }
  }
}
```

Replace every illustrative identifier with the recorded values. The binding
specification follows the existing [result evidence and execution
binding](periodic/vasp-result-evidence.md) contract. An optional
`finalization_path` selects an existing finalization that is reverified against
its original sources and policy. Omitted cases remain missing. A rerun, matching
job name, directory suffix, or newer timestamp does not replace the selected
attempt. The owner checks both the immutable study-case binding and its durable
registration target, plus the producing-domain closure and original native paths.

```sh
cmw jobs --state "$CMW_STATE" study compare --record "$STUDY_RECORD" \
  --selection "$STUDY_SELECTION" --json
cmw jobs --state "$CMW_STATE" study compare --record "$STUDY_RECORD" \
  --selection "$STUDY_SELECTION" --export "$NEW_REPORT_DIRECTORY" --json
cmw jobs --state "$CMW_STATE" study view --record "$STUDY_RECORD"
```

Process outcome, source completeness, native convergence, policy acceptance, and
input/attempt binding remain separate. Jobs `Done` is insufficient. Comparison
uses exactly the selected free energy, energy without entropy, or sigma-to-zero
energy; those definitions are never mixed. Only eV is accepted. No implicit
Hartree conversion or cross-engine comparison is performed.

The comparison exposes fixed and declared differences, including geometry, cell,
composition, species order, constraints, charge, spin, potential bytes, k sampling,
Hamiltonian and numerical controls. Matching settings do not prove equivalent
physical or magnetic states. Unknown, changed, failed, unconverged, incomplete,
or mismatched evidence retains its qualification instead of acquiring a delta.

Automatic deltas require the same reviewed geometry, cell, composition, and
constraints. Named structure variants remain side by side even when their changes
were explicitly permitted during preparation. `per_atom` only scales an otherwise
comparable fixed-structure comparison; it does not authorize subtraction across
stoichiometries or slab sizes. Surface energies, chemical-potential expressions,
and other normalizations are not implemented.

The sole rule compares each qualified case with the explicit reference using the
caller-selected absolute tolerance. Printed precision produces a delta interval;
a threshold-straddling result is unknown. Non-monotonic cases are retained. A rule
pass is only that reviewed comparison's result, not global convergence, ground
state, or model accuracy.

The CLI returns exit 2 for `PARTIAL`, `BLOCKED`, or `NOT_BOUND`. A complete
comparison can return exit 0 with `rule_result: FAIL`: completed evidence
collection does not mean the selected tolerance passed. Inspect `rule_result`
and the separate evidence domains.

Explicit export creates a new directory with JSON, CSV, and Markdown, retaining
metric, policy, normalization, coverage, source identities, and selected global
attempts. Existing destinations and scientific/state overlaps are refused.
Arbitrary text fields in CSV are escaped against spreadsheet formula injection.

## Owner routing and compact series view

Replace `--state …` with the existing `--node ALIAS --queue QUEUE --nodes-config
CONFIG` route for an enrolled endpoint. All manifests, records, selections, and
export destinations remain **owner-local**. Native files are not downloaded for
comparison. An unavailable owner has no fresh evidence; cached or cross-host
results are not silently adopted. Old workers without the negotiated
`convergence_study` capability refuse these operations explicitly.

In the existing catalogue, **S Study** opens a read-only case-binding view for the
selected enrolled study attempt. **M Series members**, **R Review**, search, job
selection, scientific tables, the Machine line, and dispatch controls keep their
existing roles. The study view does not inspect native files or implicitly run a
comparison; Escape returns to the existing catalogue/navigation state.

## Qualification boundary

Synthetic source/parser checks (L1), disposable preparation/held-registration and
benign-fixture lifecycle checks (L2), and installed endpoint/portal checks (L3) are
recorded separately in the campaign acceptance report for its exact commit.
Real scientific engines/MPI and convergence-study qualification (L4), and physical
second-node operation (L5), are unperformed. No real parameter study is run by tests.
