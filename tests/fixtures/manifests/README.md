# Phase 0 manifest examples

These are schema examples and future parser contracts, not production records.
No calculation was launched and no job directory was inspected to make them.

- `native/.cmw-manifest/` is a synthetic new VASP job held in a queue. Its three input digests
  are SHA-256 of the UTF-8 string `synthetic <filename>\n`, including the newline.
  The synthetic executable path and node names do not identify an installation.
- `j27-backfill/.cmw-manifest/` is the **expected** backfill output requested in
  issue #41, using `node-a/project-queue:J27.1` because that queue ID also
  occurs in another queue on the same node. Its expected state is `done` and
  `labort`; its inferred parent is `node-a/project-queue:J16.1`. The source
  described in the issue as `J10.7` is a historical scientific run label: the
  existing CMW queue record is `J16.1`, whose saved working directory and name
  identify that run label. This correction preserves the intended source.
  The fixture does not establish that J27.1 has exited or finished writing WAVECAR. Unknown hashes,
  timestamps, persistent node identities and launch facts use `null` placeholders;
  the requested parent relationship has not been hash-verified here. A production
  backfill must obtain objective completion and parent evidence before publishing
  these values. An empty annotations file deliberately invents no human outcome.
- `project/.cmw-project.json` is a synthetic project-configuration example for
  `project.v1.json`. The all-1/all-2 UUID strings are test values, not retrieved
  or asserted production node identities. Its `node-a` and `node-b` tokens
  illustrate permanent bindings to different nodes. The all-3/all-4 queue UUIDs,
  `/fixture-root/queues/` locators and device/inode pairs are synthetic too. One
  queue intentionally has a null UUID to represent an unenrolled source queue.
  This example is not an actual project-root configuration or enrollment.
- `states/` demonstrates every scientific-result enum, unresolved operational
  ownership, completed execution with output hashing still pending, and a
  legacy ID qualified by both node and queue. Its
  result labels demonstrate valid data shapes, not parser qualification.
- `invalid/` contains three full documents that must fail validation: an
  unqualified dotted ID, an unknown human outcome, and a missing schema version.
- `parser-contracts/` contains short, synthetic OUTCAR/stdout snippets and
  `expected` state documents for Phase 2. Phase 0 checks their expected documents
  against the state schema; it does **not** execute or implement an SCF parser.
  These abbreviated snippets are classifier-contract cases, not complete native
  VASP outputs or proof of HSE dialect qualification. Phase 2 must bind complete
  invocation/iteration evidence and add native regression samples where authorized.

All job manifest documents require schema version 1 and a provenance marker.
Native IDs use `J<n>`. Backfilled IDs may retain a flat ID or use a node-qualified
legacy ID such as `node-a:J27.1`; bare `J27.1` is invalid even for backfill.
This qualifies the manifest identity without changing an existing queue ID or
directory. The same `J4.1` suffix on two nodes remains two distinct identities.
If that suffix also collides across queues on one node, the full identity is
`<node>/<queue>:J<n>.<m>`. The short `<node>:J<n>.<m>` form is permitted only when
the project inventory resolves it to exactly one queue. A syntactically valid
short form does not establish that uniqueness.

New parent references use `J<n>` locally or `<project>:J<n>` across projects.
Legacy parent references always include the node token: `<node>[/<queue>]:J<n>.<m>`
or `<project>:<node>[/<queue>]:J<n>.<m>`, with the same ambiguity rule. Node tokens resolve only through the referenced
project's permanent `legacy_nodes` UUID mapping, never a mutable SSH/display
alias. `legacy_nodes` is required but may be empty for a project without legacy
references. Mapping uniqueness, immutability and historical queue ambiguity
require later semantic validation; the schema does not implement an allocator.
The required `legacy_queues` map binds each node/queue token pair to its existing
queue UUID (null if unenrolled), absolute owner-local state root, and original
root/database `[device, inode]` evidence. The initial binding is recorded once
in the project integrity journal and materialized in project configuration.
References contain only tokens; the initial path is a locator, not queue
identity, and later directory moves do not change references. These mappings
must never be rebound or treat aliases of the same source queue as distinct queues.
The test's synthetic collision index shows a duplicate suffix in two queues on
one node requiring full references, and a unique suffix allowing the short form.
It specifies reference-selection expectations without implementing a runtime
resolver or claiming to validate the live project inventory.

Unknown digests remain null; a zero or invented hex digest is never a placeholder
for a production hash.

The JSON Schema checks cover document shape, local conditions, enums and UTC
format. They cannot establish cross-file identity, writer authority, append-only
history, monotonic revisions, filesystem permissions, digest correctness or
scientific truth. The test suite checks fixture-set identities and annotation
sequences as example consistency, not as a production implementation.

Target registration assigns the queue job/attempt binding, so that binding is
carried in `state.json`; it is absent from the immutable authoring `job.json`.
The registration receipt remains the authority for that association. Annotation
text fields use `null` for no change and an empty string for deliberate clearing.
Parser-contract `queue_context` supplies operational status independently of the
OUTCAR/stdout evidence used for scientific classification.
