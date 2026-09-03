# ExecutionLayout v2

ExecutionLayout v2 separates scientific identity from filesystem display
identity. The filesystem path is not scientific identity.

## Path and identity contract

The canonical v2 formula is:

```text
<scientific-leaf>/[<operational-stage>/]target-<display-id>/attempts/attempt_NNN/
```

`scientific-leaf` is the human-readable project hierarchy, such as
`ground_state/sp/aa` or `excited_state/tda/ab`. An operational stage is used
only for a distinct child operation such as `orca_to_molden`. Geometry,
structure, protocol, execution-plan, source-artifact, and fragment-partition
hashes are not separate path layers.

The full target ID remains authoritative. The default display ID is the first
12 hexadecimal characters, prefixed by `target-`; its width is configurable and
recorded. The display ID is never used for equality, reuse, cleanup, or
lineage. If an existing display directory's target manifest has a different
full ID, creation fails with `SHORT_TARGET_DISPLAY_ID_COLLISION`; CMW does not
overwrite or silently lengthen the name.

Each v2 target has `target-manifest.json`. It records the layout version,
display and full target IDs, width, scientific node and operation, system,
source artifact IDs, geometry hash, fragment partition and protocol/method
identities when applicable, execution intent, plan ID, and creation
provenance. Consumers read the manifest and never recover identity from the
short directory name.

Attempt directories retain `attempt_NNN`, retry lineage, canonical/reuse state,
and cleanup-overlay behavior. Purged attempt numbers remain reserved by the
cleanup registry.

## Relative records and compatibility

V2 execution-layout records store paths relative to the scientific leaf and a
non-scientific root hint. Supplying a new root context relocates the record
without changing any scientific ID. V1 records remain readable and are never
silently interpreted as v2.

Generic target and attempt resolution supports native v1, native v2, and
migrated immutable v1 records. A committed migration registry maps each old
full path to its new full path together with the full target ID, attempt ID,
transaction ID, layout versions, and plan hash. It never resolves by a display
prefix.

## Migration and filesystem objects

`cmw migrate-execution-layout --campaign PATH --from v1 --to v2` is read-only
by default. Its deterministic plan includes target and attempt inventories,
source/destination paths, full/display IDs, collisions, filesystem-entry and
byte counts, mutable references, and a SHA-256 authorization hash.

For mixed historical trees, repeat `--include-root SCIENTIFIC_ROOT` to authorize
only explicit scientific leaves. Out-of-scope targets are recorded as
`HISTORICAL_KEEP_V1`; they are not moved or reinterpreted.

Apply requires all of:

```text
--apply --confirm-plan PLAN_SHA256 --evidence-dir EXTERNAL_DIRECTORY
```

The plan is recomputed before mutation; changed state fails with
`STALE_MIGRATION_PLAN`. The transaction journal advances through `PLANNED`,
`STAGING`, `MOVING`, `METADATA_UPDATING`, `VALIDATING`, and `COMMITTED`.
Interrupted transactions enter `RESUME_PENDING` and can be continued with
`--resume --transaction ID` or reversed with `--rollback --transaction ID`.
Scientific payload is moved with same-filesystem atomic rename. Cross-filesystem
migration fails closed. Only mutable metadata is copied to the external
evidence backup; large scientific payload is not duplicated.

Inventory uses `lstat` semantics. Symbolic links are never dereferenced,
hashed, copied, or rewritten; the link entry and exact stored link text are
preserved, including broken or externally targeted links. Hardlinked regular
files retain their inode relationship under rename, and logical and
unique-inode physical bytes are reported separately.

Immutable historical v1 records are not rewritten. Mutable current pointers
are updated transactionally, while old immutable paths resolve through the
migration registry. Scientific output hashes, geometry hashes, target IDs,
attempt IDs, and artifact IDs must remain unchanged.

## Cleanup and LED compatibility

Attempt cleanup discovers v1 and v2 attempts recursively but derives target
identity only from authoritative metadata. Its eligibility, canonical,
reference, and recovery rules continue to use full IDs. Migration does not run
cleanup; a cleanup dry run is a separate acceptance check. Historical attempts
whose stored layout conflicts with their authoritative identity are blocked
individually rather than aborting discovery of the rest of the campaign. A
reference record above the cleanup subsystem's bounded in-memory scan limit
blocks the whole cleanup plan until streaming reference support is available;
it is never skipped as if it contained no references.

Intermolecular LED dimer and frozen-fragment-reference targets use the same v2
materializer path contract. Layout display choices are excluded from execution
plan and scientific target identity, so the three-parent LED assembly and
refinalization contracts are unchanged.

## Operator examples

```bash
cmw migrate-execution-layout --campaign /path/to/campaign --from v1 --to v2 --json
cmw migrate-execution-layout --campaign /path/to/campaign --from v1 --to v2 --apply --confirm-plan SHA256 --evidence-dir /external/evidence
cmw migrate-execution-layout --campaign /path/to/campaign --resume --transaction TRANSACTION_ID --evidence-dir /external/evidence
cmw migrate-execution-layout --campaign /path/to/campaign --rollback --transaction TRANSACTION_ID --evidence-dir /external/evidence
```

Run migration only after project-specific process, lock, watcher, queue, and
terminal-state preflight proves a safe boundary.

For the completed HOF stacking migration, representative path changes were:

```text
excited_state/tda/aa/calculation/aa/aa_stack__tda/<full-target-id>/
  -> excited_state/tda/aa/target-800e066a84bb/

excited_state/tda/aa/calculation/<geometry-hash>/orca_to_molden/<full-target-id>/
  -> excited_state/tda/aa/orca_to_molden/target-ece06e04d198/

ground_state/sp/aa/calculation/aa/aa_stack__ground_state_sp/<full-target-id>/
  -> ground_state/sp/aa/target-b466171aa92c/
```

The full identities represented by the abbreviated source placeholders remain
in manifests and the migration registry; those placeholders are explanatory
only and are not accepted by the migration command. A committed transaction
can be inspected without mutation with:

```bash
cmw migrate-execution-layout --campaign /path/to/campaign --audit --transaction TRANSACTION_ID --json
cmw execution-layout-status --layout /path/to/execution-layout.json
cmw execution-layout-status --layout /path/to/execution-layout.json --verbose
cmw cleanup-attempts --campaign /path/to/campaign --superseded-only --json
```
