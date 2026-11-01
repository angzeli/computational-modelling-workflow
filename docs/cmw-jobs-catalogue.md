# Research catalogue and attempt relationships

CMW Jobs can organize existing managed attempts using explicit human metadata:
`project`, `system`, `series`, `research_stage`, `tags` and an optional display
`alias`. Submitted names, specifications, attempts, resource commitments and queue
order are unchanged. Names and notes remain searchable. Nothing is inferred from
filenames, chemistry names or scientific output.

These labels are not native parser stages, scientific identities, validated
results or workflow dependencies. Unassigned legacy attempts remain valid.
Opening a query or catalogue view never backfills metadata or enrolls a queue.

## Query an existing queue

```sh
cmw jobs --state /absolute/private/state query --text 'β-In₂S₃' --limit 50 --json
cmw jobs --state /absolute/private/state query --project interface-study --series relaxation --tag review --json
cmw jobs --node mini --queue research query --engine VASP --status Done --json
cmw jobs --nodes-config /absolute/frontend-config query --all-nodes --text 'trial' --json
```

Text is a literal Unicode substring, NFC-normalized and case-folded, matched over
submitted names, aliases and complete notes. It is not a regular expression or
shell expression. Field filters are exact; multiple tags require all listed tags.
Available filters are node/queue global identities (`--filter-node`,
`--filter-queue`), engine, operational status, project, system, series and
research-stage. `--sort id`, `id_desc` or `name` changes only presentation.

Queries return at most 100 metadata records (50 by default) and at most 512 KiB
of row projections. Names and notes in pages are previews of up to 512 characters;
`catalogue show` returns their full supported text. Matching examines the complete
stored name/note. Queries have a two-second SQL/predicate deadline and cancellable
progress checks; lock acquisition is separately bounded. They never inspect
scientific outputs or directories. These are page/query bounds, not a claim of
constant-time searching: literal matching and counts can scan queue metadata.

Use the returned `next_cursor` with the same query and `--cursor`. A change to
catalogue rows invalidates the cursor, including annotations, displayed lifecycle
status, row membership, queue order and attempt identity. Database replacement
and owner-identity changes also invalidate it. Controller/worker heartbeats and
other operational writes that leave those fields unchanged do not interrupt paging.

Each page binds a digest of the catalogue projection read in its SQLite transaction;
no persistent snapshot, cache, additional database schema or retained view state is
created. Restart the query when instructed. Paging is stable while that relevant
owner generation is unchanged, not a permanent database snapshot. Projection
validation scans metadata within the existing cancellable two-second query bound.

Federation queries up to eight explicitly connected default owner queues. Each
owner retains its own generation, cursor, timestamp and coverage; use repeated
`--owner-cursor ALIAS=CURSOR` for subsequent pages. `--all-nodes` cannot be combined
with a single local state/node/queue selector. Unreachable or unsupported owners
remain explicit partial coverage; absence from their page is not a missing result.
There is no shared atomic multi-node snapshot or copied remote queue database.

## Explicit metadata edits

First read the exact attempt and current catalogue revision:

```sh
cmw jobs --state /absolute/private/state catalogue show J23.1 --json
cmw jobs --state /absolute/private/state catalogue edit J23.1 \
  --expected-attempt-id EXACT_ATTEMPT_UUID_HEX --expected-revision 0 \
  --project interface-study --system fictional-slab --series relaxation \
  --research-stage endpoint-review --tag review --json
```

Replace a field with empty text to clear it. `--tag` replaces the tag set;
`--clear-tags` clears it. The alias is separate from the submitted name and request
specification/digest. Field values are bounded plain text (256 Unicode code
points); tags are at most 16 unique values of 64 code points each. Notes keep their
existing separate 4096-code-point contract and editor.

Edits require exact immutable attempt identity and expected catalogue revision.
A concurrent edit causes a conflict and requires a fresh read; lifecycle writes
cannot erase catalogue fields. A client-supplied `--request-id` is a UUID hex retry
identity tied to the original operation/parameters. Reuse it only for the same
request, including its expected revision. Successful replay returns the original
response. The existing node writable-connection and runtime/owner guards apply.

Catalogue schema 1 is stored in additive tables in the existing private
`queue.sqlite3`; explicit writes create them transactionally. Jobs remains at its
existing schema. Reads are read-only, and schema-1 legacy Jobs queues refuse
catalogue writes rather than silently migrating. Local unenrolled current-schema
queues can use human fields and query, but have qualified `LOCAL_UNENROLLED`
coverage and no invented global IDs. Relationships require existing explicit
node/queue enrollment; the catalogue never performs that enrollment.

## Declare attempt relationships

Relationships belong to a selected child attempt and name the parent's complete
node/queue/internal-job/attempt identity. Display IDs such as J23.1 alone are not
identities. Supported intentions are `continuation`, `repeat`, `next-stage` and
`comparison`.

```sh
cmw jobs --node mini --queue research catalogue relate J24.1 \
  --expected-attempt-id CHILD_ATTEMPT_UUID_HEX --expected-revision 1 \
  --parent-node PARENT_NODE_UUID_HEX --parent-queue PARENT_QUEUE_UUID_HEX \
  --parent-job 23 --parent-attempt PARENT_ATTEMPT_UUID_HEX \
  --kind continuation --json
cmw jobs --node mini --queue research catalogue relations J24.1 --json
cmw jobs --node mini --queue research catalogue retract J24.1 RELATION_UUID_HEX \
  --expected-attempt-id CHILD_ATTEMPT_UUID_HEX --expected-revision 2 --json
```

Identity placeholders must be replaced with exact returned values. Local parents
must exist with their exact attempt. Self/identity-alias references, duplicate
active edges and locally verifiable directed cycles are refused. Cross-owner
parents remain `UNRESOLVED_REMOTE`; the owner does not open remote paths or
pretend to verify a cross-node graph. Each attempt retains at most 100 historical
relations, including retracted edges, within a 1 MiB encoded page. Retraction retains the original edge and
creation time; it never erases history. Metadata and relationship edits share
one catalogue revision.

By default all relationships are operator intent. An optional `--evidence` JSON
file may explicitly name two existing owner-local VASP finalization records and
the exact parent/child artifact IDs:

```json
{
  "parent_record": "/owner/path/parent/finalization.json",
  "child_record": "/owner/path/child/finalization.json",
  "parent_artifact_id": "64-character-parent-artifact-sha256",
  "child_artifact_id": "64-character-child-artifact-sha256"
}
```

This bounded route verifies **recorded artifact linkage only**: the completed
record content IDs, original record locations, exact global producing attempts,
existing typed artifact IDs and the child's explicit parent-artifact edge.
Records must be regular files, no larger than 2 MiB each, and belong to attempts
in the same owner queue. Equal file contents or similar names do not join distinct
attempts. Other record schemas and cross-owner evidence are unsupported.

The result is labelled `artifact-record`, with record hashes and verification time.
It is historical evidence about a recorded edge, **not** a fresh scientific or
output-byte validation. The UI says that current scientific validity is not
assessed. Listing relations does not reopen scientific files or refresh this
historical evidence. Existing result-review/finalization/reverification APIs are
still the authority for scientific assessment. No relation satisfies a dependency,
upgrades a source status, makes a job checked, starts work or changes scheduling.

## Dashboard workflow

**/** opens Search in both local Jobs and the node portal. Type literal text and
optional filters, then **Apply** (or Ctrl-Enter). Typing does not request queries
or recreate tables. **Next page** advances the selected owner's cursor. **Clear**
returns to the unfiltered view without selecting the newest running attempt.

**T** opens the selected attempt's Catalogue: human fields, relationship history
and qualifications. **M Series members** applies its explicit series label as a
filter. Returning through Details/search reuses the identity-aware viewport
restoration contract. The existing table, scientific card and Machine line retain
their layouts; the existing message area shows matching versus total queue counts
and partial/pending coverage.

Filtering never changes queue order, admission commitments or full-queue counts.
A filtered-out selected row loses its control target. Remote older rows can be
shown as lightweight metadata, but controls remain unavailable until that exact
owner/attempt is fetched and revalidated. Superseded queries, late responses and
node disconnects cannot restore an obsolete control target. Remote hydration and
query caches are bounded. Search is an explicit observation; Apply refreshes its
matching membership while ordinary status refresh continues operational updates.

## Compatibility and qualification

Endpoints advertise `research_catalogue` schema 1 and exact typed query/record/page
models, including separate typed relationship pages. Old endpoints fail explicitly
with unsupported capability/operation; additive Python fields are not assumed
wire-compatible. Existing old lifecycle writers preserve the additional catalogue
tables in the qualified synthetic mixed-runtime test. This does not qualify every
historical implementation or authorize replacing a running runtime.

The catalogue introduces no web UI, automatic project discovery, bulk controls,
cloud synchronization, scheduler or scientific truth store. Qualification covers
synthetic metadata histories, installed core/Jobs, local and private-loopback
endpoint/portal interactions. Real scientific engines and a physical second node
remain separate qualification levels; no production data is modified to test this
feature.
