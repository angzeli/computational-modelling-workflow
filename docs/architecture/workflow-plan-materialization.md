# Workflow plan materialization

## Purpose

CMW separates scientific planning from operational execution. A
`WorkflowGraph` states which calculations and artifacts are required. It does
not choose directories, attempts, executables, or runtime resources. The plan
materialization bridge makes that scientific graph execution-ready without
launching external software.

The lifecycle is:

```text
WorkflowGraph + typed artifacts
            |
            v
       ExecutionPlan
            |
            v
WorkflowPlanMaterializer -- ExecutionRenderer
            |
            v
MaterializedExecutionNode
  JobTarget + ExecutionAttempt + ExecutionLayout
            |
            v
existing shell runner and program finalizer
            |
            v
finalize_materialized_artifacts
```

This boundary prevents domain adapters from reimplementing target identity,
attempt numbering, deterministic paths, reuse checks, or failure manifests. It
also prevents the core from acquiring chemistry or program-specific input
rules.

## Responsibilities

Scientific adapters provide:

- a validated `WorkflowGraph`;
- one `ExecutionPlanNode` for each executable calculation node;
- an explicit `ExecutionIntent`;
- validated input artifacts and planned output artifacts;
- a renderer identifier and renderer configuration.

`WorkflowPlanMaterializer` provides:

- workflow dependency and artifact-contract validation;
- renderer/task compatibility validation;
- stable target and execution-plan identities;
- deterministic `ExecutionLayout` resolution;
- pristine-attempt reuse, finalized-attempt reuse, and new attempt numbering;
- `ExecutionAttempt`, materialization, and failure provenance records.

An `ExecutionRenderer` provides:

- program compatibility checks;
- a stable `ExecutionTarget` such as `JobTarget`;
- input generation inside the supplied layout;
- declared input, output, log, and provenance paths;
- program resource and executable provenance;
- a program-specific reuse decision.

The renderer contract is generic. ORCA implements it through
`OrcaExecutionRenderer`; Gaussian, VASP, and Multiwfn can implement the same
contract without changing the materializer.

The shell execution layer continues to own process launch, signals,
environment setup, and cleanup. Materialization is prepare-only and cannot run
ORCA, Multiwfn, or another external program.

## Execution and artifact contracts

Every executable node has one explicit `ExecutionIntent`. The plan rejects
known artifact/task mismatches before a renderer is called:

| Planned artifact | Required task |
| --- | --- |
| `OptimizationArtifact` | `optimization` |
| `FrequencyArtifact` | `frequency` |
| `SinglePointArtifact` | `single_point` |
| `ExcitedStateArtifact` | `excited_state` |

Renderers may support any subset of tasks. An absent renderer, an unsupported
task, missing validated input artifacts, an unresolved path, or missing
resource/runtime identity fails closed. Execution-contract mismatches retain
the established `FAILED_EXECUTION_CONTRACT_MISMATCH` code.

Planned artifacts are not treated as results. A downstream node is ready only
when its dependencies are supplied as validated artifacts. After program
finalization produces real validation evidence,
`finalize_materialized_artifacts` delegates compatibility checks to the
existing artifact bundle finalizer and records:

- producing workflow node;
- target identity;
- attempt identity;
- execution-plan identity.

No scientific value or validation result is invented by materialization.

## Deterministic layout and lifecycle

The materializer uses the existing layout convention:

```text
<project-root>/calculation/<system>/<workflow-node>/<target-id>/
  target.json
  attempts/
    attempt_001/
      execution-layout.json
      execution-attempt.json
      materialization.json
      <renderer inputs, outputs, logs, and provenance>
```

All renderer-declared paths must be absolute and owned by the resolved attempt
layout. A renderer cannot return a current-working-directory-relative output
or redirect metadata outside the attempt. Failed preparation retains a
`materialization-failure.json` record in the allocated attempt.

Calling materialization again with the same plan, profile, and runtime returns
an unchanged pristine prepared attempt. If the existing program-specific job
metadata is reusable, the node is returned with status `REUSED`. A spent or
incompatible attempt causes deterministic allocation of the next
`attempt_NNN` directory.

## Adapter usage

An adapter binds an existing scientific ORCA stage without duplicating ORCA
logic:

```python
execution_node = orca_execution_plan_node(
    "optimization",
    orca_stage_spec,
    geometry_artifact_id=structure.artifact_id,
    planned_artifacts=(optimization_artifact,),
    outputs={"geometry": "stage.xyz"},
)

plan = ExecutionPlan(
    workflow_graph,
    (execution_node,),
    available_artifacts={"structure": (structure,)},
)
```

The serialized plan is portable across adapters because artifacts, graph
configuration, and execution intent round-trip through their established
schemas.

## Existing CLI preparation path

The existing ORCA CLI includes the prepare-only bridge; no second CLI or
executor is introduced:

```text
python -m cmw.molecular.orca.cli materialize-plan \
  --plan execution-plan.json \
  --node optimization \
  --project-root /absolute/project/root \
  --system system-id \
  --execution-config execution.yaml \
  --runtime-contract orca-runtime.json \
  --artifact-manifest finalized-artifacts.json
```

`--artifact-manifest` is optional when every required input is already embedded
in the plan. When supplied, it is either a node-to-artifact mapping or an
object containing `artifacts_by_node`. The command prints the materialized
record and exits without launching ORCA.

The existing low-level `prepare`, `finalize`, `reuse`, runtime, and lock
commands remain unchanged. Existing OPT/FREQ/SP workflows and domain adapter
plans do not need to migrate until they opt into the bridge.
