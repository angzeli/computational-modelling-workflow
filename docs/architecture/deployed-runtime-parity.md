# Deployed-runtime parity before cutover

Comparing every deployed revision with the proposed cutover revision is a
required qualification step. Preserve a deployed protection when moving to
newer ownership and provenance semantics, and record any intentional change
with its regression evidence. This check supplements the
[D31 snapshot requirements and cutover preconditions](multi-host-nodes.md).

## Recorded deployment inventory

The following audit used the saved 4 November 2026 inventory in
`~/cmw-archive/cutover-inventory-20261104/`, compared with CMW
`fdeb853e2b81bb432ed97a0736d0c9232a621ea3` on 7 November. It covers all 21 runtime
directories listed in that inventory, representing 17 distinct CMW revisions.
The source tables are `inventory.md` Mini item 5 and MacBook item 3, with
configuration and process evidence in their linked raw JSON files. This is a
dated inventory. Live deployments must be checked again during the authorized
cutover window.

The recorded Mini directories are under `~/.local/share/cmw-benchmarks/runtimes/`,
except the observer directory under `~/.local/share/cmw-observer/`. MacBook
runtime directories are under `~/.local/share/cmw/runtimes/`. A short directory
label is a candidate source identity until its deployment record and installed
content establish the association.

| CMW revision | Machine and recorded use | Evidence | Patches outside `fdeb853` |
|---|---|---|---|
| `032ddbbeb432b9c28e281458018692ef3f382b71` | Mini `032ddbb-wrapper-v1-20261102` endpoint and `032ddbb-20261102` observer; MacBook launcher, portals and disposable-node runtime | Mini directory labels; MacBook deployment source record and process paths | 0, ancestor |
| `86dde92ebc64d6fd86dbbc208aed75bda0a659d3` | Mini `86dde92-v03-20261102` and `86dde92-mpi16-20261102`; MacBook `86dde92-v03-20261102` | Directory labels only | 0, ancestor |
| `31616be474b2e2fd04ab6257c990983d46ddfcde` | Mini `31616be-j3-closure`, including J4.1 controller and worker | `deployment.json.repair_commit` and process paths | 1, `31616be` |
| `8d2e5239c20ecefa1307adb8f963599595e0863b` | MacBook retained runtime | Deployment commit and source-archive record | 0, ancestor |
| `c7515025834e5343d118a5af5722801ac0599288` | MacBook retained runtime | Deployment commit record | 0, ancestor |
| `f9591c618b3374a33e0ed6573899f6253fa72d87` | MacBook retained runtime | Deployment commit record | 0, ancestor |
| `eb1bc680f99f7581da8087b185067d434a006571` | MacBook retained runtime | Deployment commit record | 0, ancestor |
| `ca232d45054e6b656d6e8c5be9cb79d0002b9b9a` | MacBook retained runtime, also named by historical predecessor gates | Deployment commit record and archived job arguments | 0, ancestor |
| `73ff476e595763d6f14419647c39794622e0eda8` | MacBook retained runtime | Deployment commit record | 0, ancestor |
| `e60e52117a3700f5aa5704d21306b238120676c0` | MacBook retained runtime | Deployment commit record | 0, ancestor |
| `6a2d2ec3d0563b88519fb52c3761fe48d331e45e` | MacBook retained runtime | Deployment commit record | 0, ancestor |
| `5a07d61d63efe7ffe2b9006485518fc8de3c23f2` | MacBook retained runtime | Deployment commit record | 0, ancestor |
| `5dc985b20a60a1424d5ccc6d6b08404e391b0e9f` | MacBook retained runtime | Deployment commit record | 0, ancestor |
| `e1f92e46b0de422543f08f3bccdd88f11f3d7d3e` | MacBook retained runtime; associated auxiliary build environment also recorded | Deployment and post-deployment verification records | 0, ancestor |
| `c59fe0b8a00174c4b09ace7d28c2329d904110bb` | MacBook active controller, J27.1 worker and payload anchor | Deployment and installed-validation records, process paths | 0, ancestor |
| `f54cbd9abaa4af4e4fd3c234ab16841003d74118` | MacBook retained runtime | Deployment and identity-validation records | 0, ancestor |
| `8f6b8a8aa6e2e5e199836330fbdbd9ae63779eeb` | MacBook three cutover-test configurations | Qualification source-commit record | 0, ancestor |

J27.1's recorded controller, worker and payload use
`~/.local/share/cmw/runtimes/c59fe0b8a00174c4b09ace7d28c2329d904110bb/venv/bin/python`.
Its payload starts a shell that sources the VASP environment and directly
executes `mpirun` and `vasp_std`. The newer `032ddbb` launcher and portals in the
same snapshot do not identify the worker's runtime. The historical managed
VASP runner drainage defect in [issue 19](https://github.com/angzeli/computational-modelling-workflow/issues/19)
applies to the separate managed-foreground path used by J4.1.

## Patch and behavior comparison

For all 17 revisions, `git cherry fdeb853 DEPLOYED_REVISION` was inspected.
Sixteen are ancestors of the comparison target and contain zero unique
commits. The remaining revision has one unmatched patch, `31616be`. Its full
patch and the current ordinary-session worker path establish two missing
protections at `fdeb853`.

| Deployed protection | Related main implementation | Scope gap at `fdeb853` |
|---|---|---|
| A denied group send after witness exit gets up to two seconds of confirmed-absence observation | `f34e04e` introduced optional absence grace | Default zero lets the ordinary session worker propagate a denied send instead of retaining its pipe and retrying |
| The payload shell ignores TERM/INT after payload exit while publishing its receipt | `2cbc3ce` protects the anchor group during coordinated cancellation | Ordinary session cancellation still reaches the anchor group and can kill its receipt-writing child |

The replacement must use the candidate's tri-state ownership hold for
unverifiable sends and preserve durable receipt publication. The
[ownership incident](../incidents/2026-11-j4.1-clock-step.md) links these cutover
findings to the original deployed revision. The deployed tag
`deployed/31616be-j4-1` preserves its exact history.

A related branch-cleanup comparison demonstrates why a nonmatching patch ID
requires manual review. FMO frontier commit `efb1407` and main's `f1f4ec9`
contain the same 813 added/removed lines across nine files. Eight resulting
file blobs match. The ninth has additional independently integrated
excited-state imports and exports, changing the patch context. Its FMO
behavior is represented in main. That branch is outside the deployed CMW
runtime set above.

No further unrepresented revision-level patch was found in the recorded
runtime set. Git ancestry alone cannot establish installed-byte identity or
continued behavioral equivalence after later changes. The Mini label-only
identities, missing dedicated source record for runner C, and modified
MacBook VASP source remain qualification evidence gaps from the inventory.
The VASP port is a separate repository and requires its
[own ownership qualification](vasp-runner-ownership-plan.md).

## Remaining historical-runner caller gap

The ordinary CMW session worker now supplies the ownership read domain, which
retains its pipe across denied delivery. The historical VASP port runner at
51d8209 calls `signal_session(..., include_leader=False)` during TERM and KILL
cleanup without that callback or an explicit absence grace. Those calls still
inherit the historical zero default in the candidate, whereas deployed
31616be supplied a two-second default to every caller.

This is an additional caller-level qualification gap within the same unmatched
patch. An EPERM immediately after confirmed witness exit can still interrupt
historical runner cleanup instead of waiting for group disappearance. This
finding follows from the source comparison; no new runner reproduction was
performed in this change. The separate [runner cutover prerequisite, issue 19](https://github.com/angzeli/computational-modelling-workflow/issues/19)
must also qualify denied-send recovery during intended cleanup, preserving the
pending operation and termination intent. The ordinary-worker correction does
not establish parity for that historical runner path.

## Required cutover procedure

1. During the authorized cutover window, record every installed or registered
   CMW runtime on each node. Bind each configuration, launcher, controller,
   worker and payload to its actual executable and installed source record.
   Include retained deployments and runner-supplied CMW interpreters. Distinguish
   CMW package origin from a virtual environment's base standard library.
2. Record the full deployed commit, qualification record, source archive,
   relevant local changes and existing integrity evidence. Resolve directory
   labels to installed source evidence. Retain every referenced revision with
   an annotated provenance tag before removing a branch. Keep unverified
   identities pending qualification.
3. Pin the proposed target to a full commit. For each deployed CMW revision,
   run the read-only comparisons below. Save command output with the cutover
   evidence. `git cherry` reports patch-equivalent commits with `-` and
   unmatched commits with `+`. Inspect unique merge commits separately because
   their conflict resolutions may carry behavior outside ordinary patch IDs.

   ```sh
   candidate=$(git rev-parse --verify 'origin/main^{commit}')
   deployed=FULL_DEPLOYED_COMMIT
   git merge-base --is-ancestor "$deployed" "$candidate"
   git cherry "$candidate" "$deployed"
   git log --oneline --merges "$candidate..$deployed"
   git diff --stat "$deployed" "$candidate" -- src scripts
   ```

4. For every unmatched patch, inspect the complete `git show`, find related
   target commits and compare behavior through the actual current caller.
   Record stable patch IDs with
   `git show COMMIT | git patch-id --stable` when comparing near-matches.
   Explain context-only differences, partial implementations, later reversals
   and changes whose scope excludes the deployed path. Do not mark a protection
   represented from a matching subject or helper name alone.
5. Map each required deployed protection to the target implementation and a
   deterministic synthetic regression. Demonstrate the failure on the pinned
   base and success on the candidate. Apply current VERIFIED / GONE /
   UNVERIFIABLE semantics and preserve ownership and request intent at the
   action boundary. Keep additional findings separate from the approved fix.
6. Publish the per-revision matrix, remaining evidence gaps and regression
   results for cutover review. Require resolution of missing required
   protections and the separate runner prerequisites before deployment.
   Qualification and synthetic execution use the D32 sandbox, after the
   original queue's active and unresolved executions have been reconciled.
