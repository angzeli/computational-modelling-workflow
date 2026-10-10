# Live progress observations

Live progress is a read-only client feature. It consumes declared file locations,
reads bounded changes, and presents adapter-produced evidence. It never changes
Jobs status, admission, process ownership, preparation records, execution receipts
or scientific finalization. A native stop message, quiet file or threshold relation
is not a scientific acceptance decision. No controller is needed.

## Jobs controls

```sh
cmw jobs progress J1.1
cmw jobs progress J1.1 --json
cmw jobs --state /absolute/jobs-state progress J1.1 --json
# Explicit, session-local sources for an otherwise opaque wrapper:
cmw jobs progress J1.1 --engine VASP \
  --source outcar=/absolute/run/OUTCAR --source stdout=/absolute/log/stdout
```

The command performs one bounded observation, not a complete historical scan.
Large existing files may report `CATCHING_UP`; `watch` continues bounded background
chunks until caught up. The default `jobs status` command does not scan scientific
outputs. JSON schema 1 carries typed context, metric, event and source evidence.
Unknown engines report `ADAPTER_UNAVAILABLE`; ambiguous declarations report
`SOURCE_UNRESOLVED`. Files navigation remains available independently.

The main console observes only the selected managed job, approximately every four
seconds after catch-up. It never changes selection to follow another active job.
The card uses adapter-defined metric priorities; compact layouts show a few lines,
and Enter Details exposes the cached full evidence without another reader. Files,
Name, Note, Status, Elapsed and existing controls retain their purposes. No overall
convergence percentage, ETA, scientific-validity badge or automatic stop is shown.

## Association and sources

The Jobs bridge uses [delivery 1's location resolver](cmw-jobs-locations.md).
A unique declared output and stage are required for a registered default source
plan. Working directory, elapsed time, process names and external observations
are not fallback associations. State/database identity and immutable job/attempt
identity participate in the binding revision, together with declared directories
and source paths. Directory replacement requires a fresh binding.

For VASP the default plan observes the declared output's `OUTCAR` and `OSZICAR`,
and optionally `POSCAR` under the declared inputs directory. These are exact
registered filenames, not a filesystem search. Current input flags remain
unverified historical input evidence. Explicit `--source` descriptors replace the
whole default source plan for that invocation; supply all desired roles. An
explicit stdout source uses the supported electronic-table dialect. When both
OSZICAR and stdout are supplied, OSZICAR has priority and duplicate streams are not
counted twice. Overrides never edit the queue, argv or scientific provenance.

The job's declared engine selects the adapter. `--engine` is a session-local
adapter choice for otherwise unsupported labels, not proof of a native dialect.
The adapter must still qualify actual source evidence. Registered exact-file plans may retain ordered, explicitly recorded stage sources.
The ORCA plan uses exact declared native output files; it never substitutes Jobs
stdout, cwd or the newest file. See [ORCA scope and association](molecular/orca-live-progress.md).
An ambiguous workflow root still requires explicit sources rather than guessing.

## Contract for other callers and adapters

`cmw.progress.models.RunContext` is independent of Jobs. A caller can supply an
explicit binding ID, engine, stage and `SourceDescriptor` tuple without a queue:

```python
from cmw.progress.models import RunContext, SourceDescriptor
from cmw.progress.service import ObservationService

context = RunContext(
    binding_id='my-reviewed-attempt-and-source-revision', engine='VASP', stage='relax',
    sources=(SourceDescriptor('outcar', '/absolute/run/OUTCAR'),
             SourceDescriptor('oszicar', '/absolute/run/OSZICAR')),
)
client = ObservationService()
try:
    snapshot = client.poll(context)  # run outside a graphical event-loop thread
    payload = snapshot.to_dict()
finally:
    client.close()
```

Reuse a client for incremental observations; change the binding revision when its
meaning changes. The same revision with a different context is refused. `retain`
releases contexts no longer displayed; `close` releases the client's readers.
Neither affects execution. No durable history database or daemon is created.

The explicit lazy registry accepts `register_adapter(engine, factory, sources=())`.
A factory receives `RunContext` and returns an object implementing
`update(tuple[SourceUpdate, ...], now) -> ProgressSnapshot`. Source updates contain
complete lines, bounded byte/line evidence, file identity/generation, reset reason,
health and offsets. Adapters have no Store, launcher, signal or widget dependency.
`SourcePlan` entries map literal filenames or exact declared files to location roles. There is no
dynamic plugin discovery, executable configuration or installation framework.

Iteration context is an arbitrary tuple of `IterationLevel` values. Metric
observations carry semantic ID, label, definition/scope, native unit, value,
printed precision/token, original sign, evaluation context, stage/segment,
qualification, source evidence and optional criterion. Engine criteria and caller
`DiagnosticTarget` values are separate. Missing values stay null. `MetricSeries`
rejects latest/previous pairs with incompatible definitions, units, stages,
segments, criteria, diagnostic targets or comparison scope. An adapter explicitly
states the relevant comparison context: for SCF residuals it excludes only the
iteration being compared, retaining the evaluation; force summaries can compare
evaluations only with the same constraint scope.

Event identity includes binding/attempt, file generation, stage/segment, context
path and native source position. Events retained in a snapshot are bounded recent
evidence; the separate paged history view reconstructs older native contexts on demand. The fictional three-level geometry → outer →
inner adapter test demonstrates generic CLI/card extension with another native
unit; it is not CP2K support.

## VASP support and limits

The adapter reuses the existing [VASP result parser](periodic/vasp-result-evidence.md)
for numerical tokens, printed uncertainty, ordinary-mode scope and whole-atom
force definitions. The bounded native dialect is 6.6.1 ordinary SCF static and
fixed-cell relaxation, with DAV/RMM rows under the existing supported settings.
This delivery uses synthetic live fixtures; it does not establish broader native
version coverage. MD, NEB, variable-cell, hybrid/meta-GGA, noncollinear/SOC and
unknown algorithms remain outside full support.

OUTCAR establishes chronological evaluations, native labels and effective run
settings. Electronic dE/d eps, rms and optional rms(c) retain their definitions;
electronic energy is never renamed as evaluated ionic energy. Cross-file matching
uses native labels and printed energies. Temporarily unmatched evidence is pending;
complete conflicting evidence remains a conflict. Residuals in a new evaluation
never inherit the preceding evaluation's last iteration as their previous value.

Only complete supported force blocks yield force summaries. During a later SCF,
the latest completed force retains its own evaluation label; the current force
is unavailable. Whole-atom all/free/fixed vector norms reuse batch semantics.
Current POSCAR flags can support explicitly provisional diagnostic scopes, not a
verified historical constraint binding. Partial-direction flags are never used
as Cartesian masks.

Actual effective EDIFF applies to both energy-change magnitudes, with printed
resolution uncertainty retained. It is not an rms(c) target. EDIFF=0, positive or
zero EDIFFG, missing criteria and unsupported criteria remain explicit rather than
inventing ordinary force/electronic thresholds. Negative EDIFFG may provide a
conditionally qualified force threshold where whole-atom flags are available.
Native convergence at NELM is evidence, not a budget-based failure rule. Fatal,
interruption and native-convergence observations do not control execution.

## Bounded reading, lifecycle and freshness

Each regular-file reader keeps its descriptor, byte offset and partial line. One
poll reads at most 64 KiB of new data plus bounded prefix/tail sentinels (at most
256 bytes). Unchanged caught-up polls perform no data reads. Complete newline
records handle arbitrary byte chunks and split UTF-8; incomplete trailing lines
or force/energy blocks remain pending. Oversized/malformed lines have explicit
health diagnostics and bounded storage. Nonregular files are refused.

Replacement, truncation and detected in-place rewrites advance file generation.
New native run banners advance segments. Asymmetric cross-file resets remain
unresolved rather than silently pairing old and new evidence. Permissions and
mount disappearance are read failures, not scientific outcomes. Prefix/tail checks
cannot detect every same-inode interior rewrite, especially one preserving all
sampled bytes and metadata; no immutable-source guarantee is claimed.

The service bounds cached contexts, source count and global in-flight calls, with
one read per context at a time. It creates no threads itself. The console has one
progress worker; a blocked filesystem call retains its slot, even after the UI
stops awaiting it. Selection or binding changes invalidate late results. A closed
client marks in-flight readers for release when their operation actually returns;
it does not pretend a cancelled worker terminated a blocking filesystem call.

Successful observation, observed source change/growth and newly parsed evidence
have separate timestamps. Metric evidence retains its own observation time and
source position. These are client observation times, not reconstructed native
write times. A healthy unchanged poll reports no new evidence and cannot refresh
an old force's age or establish a stalled or completed calculation.

ORCA ordinary SCF/geometry observations use the same service and widgets; their
[native semantics and supported scope](molecular/orca-live-progress.md) are distinct
from VASP. CP2K parsing, ETA, prediction, automatic stopping and graph dashboards
remain deferred.


## History commands and navigation

```sh
cmw jobs progress J1.1 --history --json
cmw jobs progress J1.1 --history --limit 25 --json
cmw jobs progress J1.1 --history --cursor 'CURSOR_FROM_PREVIOUS_PAGE' --json
cmw jobs progress J1.1 --history --parent 'ENTRY_ID_FROM_PAGE' --json
cmw jobs progress J1.1 --history --parent '*' --stage relax --segment 2 --json
cmw jobs progress J1.1 --history --history-polls 128 --json
```

`--parent` selects immediate children; `*` selects all depths. Each command starts
an independent observation session and performs at most `--history-polls` bounded
slices (default 32, range 1–4096). `--limit` bounds returned entries (default 50,
maximum 200). If the work budget expires, `LOADING` / `PARTIAL` is returned with an
unknown total. Increasing the budget reconstructs farther; a loading response does
not persist a parser checkpoint between CLI invocations.

In `watch`, G opens Progress History for the selected job. Enter opens children
(or Details for a leaf), D opens full metrics/criteria/timing/source evidence,
Backspace returns to the parent, `[` / `]` select older/newer pages, S cycles stages
and V cycles segments. Esc/Q returns to the console; Ctrl-C detaches. P=Dispatch,
O=Order, N=Note and F=Files retain their console meanings, and those letters remain
ordinary text in the note editor. History reading shares the selected card's
observation client and single background worker. Details uses cached entries.

Entries have stable keys; updates preserve the selected older entry, scroll
position and open Details. New output does not move selection to the tail.
Queued/unsupported jobs show explicit empty states. Failure or cancellation does
not invent terminal convergence. A source replacement invalidates navigation;
earlier open Details is labelled as cached evidence rather than rebound silently.

## Meaning of current, previous and complete history

- **Current** is the latest supported structural context observed in the selected
  binding, not necessarily the engine's instantaneous state. Catch-up is explicit.
- **Latest / previous metrics** retain delivery 2's comparable context, source,
  definition, constraint and criterion rules. A new SCF evaluation has no previous
  residual inherited from the preceding evaluation.
- **Latest / previous completed durations** refer to the two most recent complete
  force/energy evaluations within the current native segment. Each retains its
  evaluation label and timing source. Missing duration remains unavailable.
- **Complete coverage** means the requested history query has been reconstructed
  through the currently observed readable source horizon. It does not mean the
  calculation finished, every timing exists, or all rows are returned in one page.
  `total`, `next_cursor`, `previous_cursor`, `has_more` and `retained` distinguish
  the filtered history from a bounded page. Missing/unreadable sources and pending
  catch-up cannot claim complete coverage.

Chronological evaluation identity is distinct from the native printed label.
Repeated labels are retained. Explicit trial/line-search text is evidence of a
trial evaluation; an accepted optimizer step is not inferred. Stages and native
segments remain visible boundaries. Separate Jobs attempts are never joined from
names, notes or a word such as “continuation”.

## Timing evidence

The supported modern OUTCAR grammar is `LOOP: cpu time ...: real time ...` and
`LOOP+: cpu time ...: real time ...`, with shared numerical-token parsing,
whitespace tolerance and complete newline records. CPU and real/wall values are
separate observations. A LOOP is associated with its open native Iteration header;
it may precede that iteration's energy. LOOP+ requires the corresponding complete
native force/energy evaluation before the next header/segment/footer. Late timing
updates the same entry. Duplicate, malformed, negative, unassociated or unsupported
records leave timing unavailable while independent metrics remain usable.

These structural rules are based on the [official-forum OUTCAR example](https://vasp.at/forum/viewtopic.php?p=27003)
and [LOOP/LOOP+ comparison](https://vasp.at/forum/viewtopic.php?t=19225).
Fixtures exercise scientific-notation tokens synthetically; they do not establish
additional native-version support. Old VPU/CPU alias rows are unsupported.

LOOP+ is inclusive outer-loop timing. It is never added to nested LOOP intervals.
Final Total CPU/Elapsed footer values belong to the segment as cumulative totals;
they never manufacture per-evaluation durations. Preparation and other work
outside native measured intervals are not reconstructed. No CPU value substitutes
for wall time, and no cumulative-clock subtraction or interpolation is used.

An observer interval is explicitly `source=observer`,
`quantity=observation-interval`, measured with an injected monotonic clock in one
client session. It describes time between observations of that context, not its
complete execution duration. Buffered output and an unknown execution start are
stated qualifications. Several completed iterations arriving in one flush receive
no artificial zero-length duration. Intervals freeze when the context closes;
clock resets or changed clock identities are unavailable, never clamped to zero.
A fresh client cannot reconstruct observer-only intervals that were never saved.
Native timings remain reconstructible from native records. File modification
times are used only for source-change guards, never for execution timing.

## Adapter history extension and bounds

The additive `HistoryEntry` contains binding/attempt identity, chronological
sequence, arbitrary context path and parent key, native label, stage/segment,
completion/coverage, metrics, timings and source references. Upserts preserve the
entry key and sequence. `Timing` explicitly separates clock, quantity, source,
value/unit, boundaries, completeness and uncertainty. `HistoryColumn` descriptors
choose ordinary fields, metric IDs or timing; optional `kinds` selects columns for
different context types. Neither the renderer nor the service dispatches on engine.

An adapter may implement `drain_history()` to return bounded chronological entry
upserts after each update. Optional `set_observation(monotonic_now, historical=...,
clock_id=...)` supplies session timing qualification; replay is always historical.
Optional `read_roles()` and `has_pending_work` provide bounded source backpressure.
The VASP adapter uses this to align differently sized OUTCAR/OSZICAR streams;
the fictional geometry → outer solver → inner solver fixture uses its own C/s
metric and the same CLI, widgets, paging and details.

`ObservationService.history(context, cursor=None, limit=50, parent_id=None,
stage=None, segment=None)` (also `history_page`) returns a `HistoryPage`.
`snapshot(context)` reads the cached live card; `release_history(context)` stops
requesting replay work while retaining the card. Closing a view cooperatively
releases replay resources after an outstanding read actually returns.

Memory is bounded to eight cached contexts, 256 recent entries/context, four
cached pages/context, 200 entries/page, 64 selectable stage/segment boundaries and
one active reconstruction/context. Older segments remain accessible through all-
segment paging or explicit CLI filters. An uncached old page reconstructs from
the beginning using the existing reader/adapter in bounded slices; no unbounded
in-memory index, telemetry database or durable observer history is created.

Each history slice performs at most one live and one replay reader budget per
source: 64 KiB each plus up to 256 sentinel bytes each. Unchanged cached pages do
not reread source content. Global in-flight work remains bounded to two contexts,
one operation per context; the console uses one worker. VASP processes at most
2,048 queued lines/update, retaining one reader batch/source, 16 correspondence
evaluations with at most 1,024 SCF rows each, and bounded pending history upserts
(backpressure threshold 32,802). Existing force/atom and POSCAR limits still apply.

Cursors bind query filters, attempt/source identity and file generation. Within
one client, ordinary appends preserve navigation. Across clients, size/mtime/ctime
must also remain unchanged; even an append conservatively requires restarting
navigation with no cursor. This prevents a new reader's generation counter from
accepting a cursor for an in-place rewritten file. Guards reuse existing stat
metadata, not full-file hashes. Arbitrary unsampled interior rewrites still carry
the reader's documented residual detection limits.


## Adding a bounded engine adapter

1. Declare source roles/resolution through the existing location contract. A
   `SourcePlan` with a literal filename resolves within a declared directory;
   `filename=None` uses an exact declared file. `stages=True` retains ordered
   recorded stage sources, with `SourceDescriptor.stage` carrying each label.
2. Implement a bounded adapter and explicit dialect detection. Reuse the supplied
   `SourceUpdate`/`SourceLine` stream; do not add a file scan, poller or UI thread.
3. Emit shared contexts, metrics, criteria, timing and source evidence with native
   semantic IDs, units, definitions and scope. Unknown values remain null; a
   printed convergence flag and a numeric comparison are separate evidence.
4. Register a lazy factory and its source plan in the small internal registry.
   No executable plugin discovery or engine import in Store/scheduling is needed.
5. Add positive, negative, chunking, parity and installed tests. Exercise a metric
   without a target, unknown units, nested contexts, stages/resets and missing
   timing through the generic card, history, details and JSON.
6. Update CAPABILITY_MAP.md and the engine's scope documentation.

For explicit CLI declarations, `--source ROLE@STAGE=/absolute/file` carries a stage
and produces the distinct role `ROLE:STAGE`. Repeat in known workflow order;
`--stage` remains a history filter. Plain `ROLE=/absolute/file` is unchanged.
These declarations are session-local observation associations, never edits to the
queue or scientific provenance.

Delivery 4's shared adjustments are exact-file/staged source plans, additive
source stage metadata, and this generic CLI syntax. Existing VASP directory plans
remain intact; cross-engine binding and fictional-adapter tests cover the changes.
The fictional fixture distinguishes maximum components from atomic vector norms,
uses another native unit and a target-free metric, and traverses three levels,
stages, resets and optional timing. It is not a CP2K parser.

A future CP2K adapter could emit a FORCE_EVAL context above geometry and outer/inner
SCF levels, each with its own metrics, source evidence and criterion scope. Multiple
FORCE_EVAL identities would remain distinct contexts within declared stages and
segments. The existing variable-depth model and descriptor-driven presentation
already permit that shape; no CP2K output support is implemented here.
