# Superseded-attempt cleanup

`cmw cleanup-attempts` is the generic, campaign-level lifecycle operation for
reclaiming storage from execution attempts that CMW can prove are superseded.
It is deliberately not a recursive-delete wrapper. Planning, canonical-attempt
resolution, artifact/reference analysis, process ownership, path containment,
transaction recovery, and post-cleanup validation are Python-owned semantics.

The v1 scope is intentionally narrow:

```sh
cmw cleanup-attempts \
  --campaign /path/to/campaign \
  --superseded-only
```

`--superseded-only` is mandatory. There is no force option, and v1 cannot
select a canonical/current attempt. The default mode is `minimal-provenance`
and the default operation is a dry run. Without `--apply`, the command writes
nothing.

## Campaign and canonical-attempt contracts

A campaign path resolves to one real, non-symlink directory containing a
`calculation/` execution tree or a campaign manifest. A campaign id can be
resolved uniquely through roots listed in `CMW_CAMPAIGN_ROOTS`. Ambiguous,
missing, filesystem-root, and home-directory selections fail closed.

The command consumes the existing deterministic layout:

```text
campaign/
└── calculation/<system>/<node>/<target>/
    ├── target.json
    └── attempts/attempt_NNN/
        ├── execution-layout.json
        └── job.json or another finalized result manifest
```

An optional `.cmw/campaign.json` can identify the campaign and explicitly map
targets to `canonical_attempt_id`, `current_attempt_id`,
`finalized_attempt_id`, or `reusable_attempt_id`. An explicit choice is valid
only when the selected attempt still passes the generic finalized/reusable
contract. Without an explicit choice, exactly one intact reusable attempt must
exist for the target. Multiple reusable attempts are ambiguous; the highest
attempt number is never assumed to be canonical.

A reusable attempt must match its target identity, have successful execution
and valid scientific status when those fields are present, and retain every
required artifact at its recorded size and SHA-256. A separate canonical
reusable attempt for the same target is required before any older attempt is
eligible. Explicit supersession metadata is evidence, not a way to bypass
canonical validation.

## Eligibility and reference graph

The planner inspects structured campaign, target, attempt, result, workflow,
artifact, reuse, lineage, recovery, watcher, and cleanup metadata. It records
the authoritative field and source for each reference. It does not use textual
search as a correctness decision.

Cleanup is refused when an attempt is nonterminal, active, canonical,
ambiguous, the only finalized result, recoverable after an execution or
finalization-only failure, or lacks a separate finalized reusable
replacement. A payload path or scientific-artifact reference from current
authoritative state also blocks cleanup. Attempt-id-only history can remain
resolvable through the cleanup overlay and compact tombstone; it is retained
as reference-graph evidence in the plan.

The campaign as a whole is read-only while cleanup plans are applied. Active
or uncertain campaign, target, attempt, watcher, or orchestrator ownership
blocks all candidates. Local dead locks are diagnostic evidence and do not
become proof of activity. Recorded live or unverifiable PIDs block cleanup.
On systems with `lsof`, each otherwise eligible candidate is checked for a
writable open descriptor. If that optional probe is unavailable, the fact is
reported; the canonical lock and PID gates remain authoritative.

## Deterministic cleanup plan

Every invocation first constructs a plan containing campaign identity and
state hash, mode, candidates, blocked and already-cleaned attempts, canonical
replacements, byte/file counts, reclaimed-byte estimates, retained and removed
paths, reference evidence, process/lock evidence, and tool version. Its
SHA-256 is calculated from canonical JSON. No creation timestamp or random
transaction id participates, so unchanged dry runs produce the same hash.

Immediately after obtaining the exclusive cleanup lock, an apply recomputes
the complete plan. Any changed campaign, reference, ownership, inventory, or
canonical-selection evidence returns `STALE_PLAN` before attempt data moves.

## Minimal-provenance mode

Apply the default mode with:

```sh
cmw cleanup-attempts \
  --campaign /path/to/campaign \
  --superseded-only \
  --mode minimal-provenance \
  --apply
```

The original attempt directory is atomically moved into transaction staging
and replaced by a directory containing only `attempt-tombstone.json`. The
tombstone retains bounded operational and scientific identity: campaign,
target, and attempt ids; terminal/failure status; exit code; available timing;
the canonical replacement; recorded command, input, and spec hashes; runtime
identity; source geometry/protocol identity; scientific artifact identity;
original counts; authoritative hashes already present in the artifact
manifest; cleanup identity/reason; and one bounded diagnostic tail.

Full ORCA output, GBW, Molden, `.mwfn`, cube, scratch, and complete session-log
payloads are not retained. The default tombstone limit is 256 KiB and the
default diagnostic tail is 16 KiB. The command refuses a candidate rather than
silently exceeding `--tombstone-max-bytes`. `--diagnostic-tail-bytes 0`
disables the optional tail.

## Purge mode

Purge is irreversible and requires the exact 64-character hash displayed by a
fresh dry run:

```sh
cmw cleanup-attempts \
  --campaign /path/to/campaign \
  --superseded-only \
  --mode purge \
  --apply \
  --confirm-plan <full-plan-sha256>
```

Purge removes the complete attempt directory and creates no attempt-local
tombstone, diagnostic tail, inventory, or scientific metadata. Its committed
transaction directory retains only bounded transaction state and a
campaign-level receipt containing transaction/mode/tool/time/reason, aggregate
attempt and byte counts, pre/post campaign hashes, plan hash, and final state.
The staging manifest and registry backup are removed before commit because
they contain attempt-specific paths.

The cleanup registry still records that an operational attempt id was purged
and which canonical attempt superseded it. This prevents missing attempt
payloads from appearing as unexplained corruption and prevents a purged
`attempt_NNN` identifier from being allocated again. The overlay is not a
scientific artifact and does not affect target or artifact hashes.

## Filesystem safety

Planning resolves and validates every campaign, target, and attempt path.
Cleanup never invokes a shell deletion command. The Python remover uses
`lstat`/non-following directory traversal, unlinks links rather than their
targets, safely removes broken links, refuses links escaping an attempt,
refuses unsupported filesystem objects and mount-boundary traversal, and can
never select the campaign or target root. A shared hardlink removes only the
candidate's directory entry; shared bytes are excluded from the reclaimed-byte
estimate. Content-addressed or externally referenced payloads remain blocked
by the reference graph.

## Transaction and recovery lifecycle

Applied cleanup uses `.cmw/cleanup.lock` and a transaction under
`.cmw/cleanup/transactions/<transaction-id>/`. State is atomically persisted
through:

```text
PLANNED -> STAGING -> STAGED -> METADATA_UPDATING -> COMMITTED
                         \-> ROLLING_BACK -> ROLLED_BACK
                                            \-> FAILED
```

Candidate directories are renamed into same-campaign staging before a
tombstone or registry overlay is written. Atomic JSON replace is used for the
transaction state, tombstone, registry, and purge receipt. Canonical reusable
attempts are resolved again before irreversible staged deletion.

An ordinary failure before staged payload deletion rolls back paths and the
prior registry. A process crash leaves discoverable state. The next applied
cleanup rolls back a pre-metadata transaction or resumes staged deletion when
the registry proves that metadata commit completed. A partial deletion is
marked `FAILED` with recovery required and remains resumable; it is never
reported as committed while staged bytes are unknown.

## Cleanup registry and compatibility

`.cmw/cleanup/registry.json` is an additive operational overlay. Entries are
`COMPACTED` or `PURGED` and record transaction, mode, retained path when any,
canonical replacement, and completion state. Legacy campaigns need no cleanup
metadata until this command is applied. Existing materialization, finalization,
reuse, and resume readers continue to ignore tombstones naturally, while the
shared attempt allocator consults the overlay so purged identifiers are not
reused.

Repeated dry runs are deterministic. Repeated applies do not allocate attempts
or duplicate tombstones/transactions; already compacted or purged entries are
reported as no-op state.

## Output and exit codes

Human output shows plan identity, campaign/mode, aggregate storage, candidates,
and blocked attempts. `--json` emits one structured document for both success
and failure.

| Code | Meaning |
| ---: | --- |
| 0 | successful dry run or apply |
| 2 | invalid arguments or missing purge confirmation |
| 10 | no eligible attempts / idempotent no-op |
| 11 | attempts exist but all are ineligible or ambiguous |
| 12 | plan became stale before apply |
| 13 | campaign ownership is active or uncertain |
| 14 | campaign or attempt path is unsafe |
| 15 | transaction failed and was rolled back |
| 16 | irreversible deletion began and recovery is required |

Cleanup does not start ORCA, Multiwfn, or any other scientific executable and
does not create a scientific result.
