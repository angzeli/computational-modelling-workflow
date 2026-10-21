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
# Explicit, invocation-local sources for an otherwise opaque wrapper:
cmw jobs progress J1.1 --engine VASP \
  --source outcar=/absolute/run/OUTCAR --source stdout=/absolute/log/stdout
```

The command performs one bounded observation, not a complete historical scan.
Large existing files may report `CATCHING_UP`; `watch` continues bounded background
chunks until caught up. The default `jobs status` command does not scan scientific
outputs. JSON schema 1 carries typed context, metric, event and source evidence.
Unknown engines report `ADAPTER_UNAVAILABLE`; missing declarations report
`SOURCE_UNRESOLVED`, and incompatible declarations report `SOURCE_CONFLICT`.
Files navigation remains available independently.

The main console observes only the selected managed job, approximately every four
seconds after catch-up. It never changes selection to follow another active job.
The scientific card uses adapter-defined groups and fixed label/value columns.
VASP shows **SCF**, **FORCE**, and **ELECTRONIC**, with the electronic group on two
physical lines at compact widths. ORCA supplies its own gradient/displacement and
SCF descriptors. The dashboard uses bordered scientific tables at ordinary
compact widths. Available height selects eight rows with a source-footer rule,
seven without that rule, or six with evidence integrated into the bottom edge.
Compact layout reserves at least three managed rows and the operational controls;
only genuinely narrow or short viewports fall back to borderless text. CLI and
Copy snapshots remain undecorated. The generic renderer
does not infer engine semantics. The current native context remains separate
from the latest complete row: SCF 9 may be active
while the complete electronic values are labelled `@SCF8`. The native evaluation
label includes effective positive NSW when available, such as `Eval 7/20`; missing or unqualified NSW leaves `Eval 7`. Latest force always
retains its evaluation (for example `@E6` while evaluation 7 is current), and a
static calculation has no ionic force stopping target.

The card preserves units, source qualification, checked frontier and scientific-data
age. Details/History retain previous values, trends, timing, rms(c), source positions
and full warnings. Missing current rms(c) is absent; an older value cannot fill it.
`*` identifies provisional evidence/targets and `?` a precision-uncertain relation;
neither is a scientific outcome badge. Source conflicts, unsupported evidence and
cached-read errors remain visible. Direct selection freezes only the selected
text revision; observation and execution continue.

Arrows and signed deltas describe only latest/previous pairs accepted by the
existing comparison contract. An equal pair shows `= 0`; a change within the
supplied printed resolutions shows `≈ Δ…` without a directional arrow. Small
nonzero changes are not rounded to a false zero. Display precision expands when
necessary to distinguish an otherwise identically rounded pair. Increasing force
is not failure, and decreasing force is not convergence. No trend is inferred
across incompatible definitions, units, criteria, constraints, stages, segments or
comparison contexts. Details explains unavailable comparisons.

Enter Details exposes the cached full evidence without another reader. Files,
Name, Note, Status, Elapsed and existing controls retain their purposes. No overall
convergence percentage, ETA, scientific-validity badge or automatic stop is shown.

### Source evidence and independent targets

A missing electronic measurement does not hide a known target. The card obtains
EDIFF and EDIFFG independently from qualified effective OUTCAR settings, falling
back only to explicitly declared provisional INCAR evidence. Contradictory,
unsupported or other-segment settings cannot restore a target. Known effective
settings remain visible even when the calculation or comparison is unsupported;
this does not claim criterion satisfaction. A negative EDIFFG without a supported
force/mask comparison is labelled `unapplied`; static calculations remain target N/A.

Ordinary VASP 6.6.1 fixed-cell relaxation supports ISIF 0 and 2 within the existing
electronic-mode and special-control restrictions. With a separately declared,
validated whole-atom POSCAR mask, the compact FORCE row shows only `Fmax movable`
and its conditional EDIFFG target. Completely fixed atoms do not control this
comparison. All/fixed force norms remain in Details/JSON; absent or partial-direction
constraints never turn the all-atom norm into a movable-force criterion. Input
declarations retain their attempt association and current-input qualification.
For newly queued supported `run-vasp.sh` routes, the automatic staged-input
identity described below additionally checks the exact POSCAR bytes. Force
comparisons remain provisional and never establish scientific acceptance.

JSON, Details and Copy distinguish undeclared, empty, incomplete/catching-up,
unparsed, malformed, unsupported, unavailable, source-local, partially matched,
matched and conflicting electronic evidence. Source diagnostics count parsed
lines per reader generation and complete/usable rows in its latest evaluation.
Retained earlier metrics keep their own row context and timestamp. An empty
source never receives a source-local claim. An explicit unsupported native banner
cannot be matched merely because its numerical energies agree.

The card's data age is the observation time of the displayed complete electronic
row, never a newer OUTCAR header or setting. Details and Copy separately expose
observation attempt, successful source read, observed file growth, complete row,
OUTCAR structural/setting evidence and correspondence advancement times. These
are client observation times, not reconstructed native write times. An unchanged
poll advances the read clock without refreshing electronic evidence. Bounded
readers and parser queues remain unchanged; no per-poll database writes occur.

## Association and sources

The default is automatic: the Jobs bridge consumes the existing
[location resolver](cmw-jobs-locations.md), recognized runner argv, recorded
execution layouts and supported workflow metadata, then reconciles these with
any sources declared at add time. A unique compatible binding starts bounded
observation immediately. Opening Sources or pressing Validate is unnecessary
for ordinary supported jobs.

Reconciliation compares semantic role, stage and exact canonical path; no origin
silently wins. Identical declarations are combined while retaining their origins.
Different paths for a required role, including disagreement between runner and
layout, produce `SOURCE_CONFLICT` without opening either source. An unavailable
optional source does not invalidate the required binding; conflicting optional
sources are omitted with a warning. Different ORCA stages remain separate. A
single VASP stage retains native source roles; multiple VASP stages are refused.

For VASP the default plan observes `OUTCAR` and `OSZICAR` under the declared
output, plus optional `POSCAR` and `INCAR` under separately declared inputs. These are literal
registered filenames. ORCA uses exact declared native output files and supported
stage metadata, never Jobs stdout or a workflow root without exact stage files.
See [ORCA scope and association](molecular/orca-live-progress.md).

A declared path stays established when a file is not yet created, a mount is
unavailable or access is denied. The reader reports the actual availability or
access problem; it does not invite configuration merely because evidence is
missing. An unsupported parser/dialect similarly preserves the binding. Neither
case permits substitution of another nearby file. Cwd alone, job names, process
activity, elapsed time and newest-file searches never establish a source.

### Portal Sources

On the main Jobs screen, **S** opens Sources for the selected immutable attempt.
A working automatic association opens in inspection mode, showing origin and
status; Details exposes exact paths and qualifications. **Use Different Sources**
opens the explicit form. Unresolved jobs open in configuration mode; conflicting
jobs expose the declarations for review. The main card remains concise and
shows `[S] Configure Sources` or `[S] Review Sources` when needed.

Fields come from the registered adapter source plans. A caller-selected VASP
output directory can **Propose Exact Paths** for `OUTCAR` and `OSZICAR`. Review
those paths before use; optional `POSCAR` and `INCAR` need their own declared input paths and
are never invented from that output directory. The separate `stdout` field is a
genuine electronic-source alternative; do not put stdout in the OSZICAR field. ORCA uses its native output field
and stage where applicable. Paths are literal, including spaces and Unicode;
no shell expansion, wrapper execution or directory search occurs.

**Validate** checks roles, stages, required/optional evidence, bounded file
metadata, accessibility and identity. It does not activate or fully parse the
output. **Use This Session** rechecks the selected state/job/attempt, declarations
and filesystem identities before starting normal bounded observation. Conflicts
also require the explicit review checkbox. Such an override retains the conflict
and the qualification that scientific lineage is unverified; it changes neither
the recorded runner nor the persisted declaration. **Use Default** discards this
session's override and returns to declaration reconciliation.

The in-memory selection belongs to the canonical Jobs state/database identity,
internal job ID, immutable attempt, engine and binding generation. Switching jobs
does not transfer it; returning to the same valid attempt can reuse it. Closing
and reopening the portal clears session selections. File/directory replacement,
symlink redirection or changed declarations require fresh validation; truncation
and native generation changes retain the existing reader protections. Late
asynchronous results cannot update another selected attempt. Sources shares the
existing bounded progress worker and observation service, without another poller
or per-poll database writes.

An opaque shell `-c` job without declarations remains `SOURCE_UNRESOLVED`, even
when Files opens its working directory and execution observation verifies its
worker. S supplies the explicit fallback for that portal session. F remains
folder navigation, and G opens history; History's S still selects a stage.
Shortcut letters typed inside path, note or reason inputs remain literal text.

### Declarations for future jobs

`--progress-source` is optional. Use it before `--` when the output paths are
known but a wrapper does not expose a recognized declaration:

```sh
cmw jobs --state /absolute/isolated-jobs add \
  --name "Opaque VASP wrapper" --engine VASP --cwd /absolute/work --hold \
  --progress-source outcar=/absolute/run/OUTCAR \
  --progress-source oszicar=/absolute/run/OSZICAR \
  -- /bin/bash -c 'exec /absolute/work/trusted-wrapper.sh'
```

This only enqueues the caller's command. Existing argv handling and launch checks
are unchanged. `ROLE@STAGE=/absolute/file` supplies an explicit stage. Add-time
validation checks literal absolute paths, registered roles, duplicate or
inconsistent declarations and the required source set; files need not exist.
The registered `stdout` alternative can supply the VASP electronic stream in
place of `oszicar`. Optional sources retain their registered meaning.

The additive `progress_sources` job metadata has declaration schema 1, the new
attempt ID, engine, role/path/stage/optional descriptors, origin
`user-declared-at-add`, and qualification `caller-declared; scientific lineage
unverified`. It is immutable after add and survives Store reload, separate CLI
processes, portal/controller restarts and ordinary lifecycle writes. It does not
freeze inodes before outputs exist or change Jobs control schema 1/2/3 handling;
legacy records remain absent without read-only backfilling. Existing active
legacy-state migration safeguards still apply. There is no source-edit operation
for an existing attempt.

Compatible persisted declarations are observed automatically by the portal and
`cmw jobs progress JOB_ID --json`; disagreements with automatic declarations
remain conflicts. JSON, Details and Sources retain origin and qualification.
A source declaration identifies a file to observe, not verified execution
lineage, scientific acceptance or finalization provenance.

### Automatic POSCAR identity for future VASP attempts

Both `jobs add` and the programmatic `Store.add` API use the same optional
registration hook for the recognized `run-vasp.sh --input … --output …
--managed-foreground` route, including its controlled-suspension variant. No
extra monitoring option is needed. Static and fixed-cell preparation share this
route; ordinary and Selective Dynamics POSCAR files retain their exact bytes,
atom order, geometry and flags.

At enqueue, additive `progress_sources.vasp_input` metadata records the immutable
Jobs attempt UUID, exact argv and input/output paths, output-parent identity and
the prepared POSCAR SHA-256, byte count and source stat identity. This is a
**prepared snapshot**, not evidence that staging or execution occurred. The
output must not yet exist, matching the supported runner's exclusive staging
contract. Missing, inaccessible, oversized or ambiguous evidence records a
monitoring limitation without preventing enqueue or launch. The existing Jobs
database schema and scheduler behavior are unchanged.

During read-only resolution, CMW consumes only the declared output's bounded
`RUN_METADATA.txt` staging prefix. It requires an ordered STAGING/RUNNING boundary,
all four staged input identities, matching paths and mode, POSCAR hash/size and
source stat, and a staging timestamp within the recorded Jobs attempt. It then
checks the actual `<output>/POSCAR`. The bounded progress reader verifies the
exact bytes it consumes against the immutable digest before exposing a complete
constraint mask. The origin is `attempt-staged-input`; local runner evidence is
not authenticated execution or scientific acceptance. Terminal failure does not
undo a completed staging step. Finalization retains its separate, stricter
terminal/receipt and scientific checks.

The same immutable source metadata survives lifecycle saves and client/controller
restarts. A retry is a new enqueue with a new Jobs UUID and a fresh input identity;
there is no in-place Jobs retry operation. Historical observation continues to
use its original staged path and expected digest even after the prepared input
changes. Missing, replaced or modified staged bytes remain unavailable, with no
fallback to the latest prepared POSCAR. Snapshotting the content is unnecessary;
retention of the original runtime file is still required. Editing prepared inputs
between enqueue and staging deliberately leaves the optional binding unqualified.

An explicit incompatible POSCAR path remains a visible optional-source conflict.
A failed staged expectation cannot be satisfied by an alternate add-time POSCAR.
Invocation-local/session source overrides retain their existing explicit,
unverified qualification. Opaque wrappers, layout-only declarations and runner
invocations without the managed-foreground contract (including dry runs) have no
registered staging contract and retain their existing current-input association;
CMW does not invent an executed-input binding for them. CMW preparation publishes
inputs but does not enqueue or launch VASP, and the generic workflow materializer
has no standard VASP renderer.

Legacy attempts are never backfilled. In particular, a historical OUTCAR/OSZICAR/
stdout declaration without POSCAR does not acquire a durable POSCAR association
from observation. Later historical association needs separately authorized exact
attempt/runner records, original staged POSCAR identity and source closure; a
current file or matching atom count alone is insufficient. No historical binding
edit command is introduced by this feature.

### Invocation-local CLI sources

The existing `progress --source` syntax still selects sources for one call only;
supply all desired roles. It does not change an open portal or the Jobs database.
The explicit choice retains any conflict with recorded declarations. A partial
single-stream invocation remains supported. VASP stdout uses its supported
electronic-table dialect; when both OSZICAR and stdout are supplied, both are read incrementally and
compared. An empty OSZICAR cannot suppress usable declared stdout; logical rows
are not counted twice. `--engine` selects an invocation-local
adapter for an otherwise unsupported label, not proof of a native dialect;
persisted sources cannot be reinterpreted as a different engine.

Identical diagnostics appear once in stable order. Distinct resolver, access and
integrity errors remain available in Details/JSON. Execution observation,
source-binding health, parser/read health and scientific convergence remain
separate evidence throughout.

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

The explicit lazy registry accepts
`register_adapter(engine, factory, sources=(), source_roles=())`.
A factory receives `RunContext` and returns an object implementing
`update(tuple[SourceUpdate, ...], now) -> ProgressSnapshot`. Source updates contain
complete lines, bounded byte/line evidence, file identity/generation, reset reason,
health and offsets. Adapters have no Store, launcher, signal or widget dependency.
`SourcePlan` entries map literal filenames or exact declared files to location
roles. `sources` defines the automatic plan; `source_roles` registers additional
explicit roles, and a plan may register required-role alternatives. Required and
optional roles are shared by add-time validation and the Sources form. There is
no dynamic plugin discovery, executable configuration or installation framework.

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

## Active electronic verification

Only exact declared attempt/stage sources are eligible. CMW never searches nearby
files or treats an arbitrary operational stdout as a unique native run. OSZICAR and
stdout retain independent bounded parsed evidence. Stable selection follows the
most advanced supported frontier, retaining the current source on a tie; usable
OSZICAR remains the normal initial choice. Source changes are recorded. Missing,
empty, inaccessible, malformed and contradictory evidence are distinct states.

Each relevant append triggers correspondence checks against retained unmatched
records, including complete SCF prefixes before a final energy summary or force
block exists. OUTCAR row correspondence checks ordered iteration labels, supported
algorithm evidence and energy at printed resolution, within the declared binding,
source generations and native segment/evaluation association. It does **not** prove
that OUTCAR independently printed stdout's dE, d eps, rms or rms(c). Duplicate
OSZICAR/stdout rows can additionally compare equivalent signed electronic fields.
Ionic-summary d E, band d eps and generic energy estimates are not interchangeable.
Details/JSON identify comparison fields, precision policy and source evidence.

For example, stdout through SCF 8 with OUTCAR through 6 shows the latest complete
row as source-only and the checked frontier as 6. Rows 7–8 are rechecked when OUTCAR
advances, without a key press, new reader or observer restart. Their existing
history identities are promoted rather than duplicated. An empty or unbound
counterpart retains its exact limitation. Complete contradictions remain visible
until the relevant generation/binding boundary changes; a later agreeing row
cannot silently remove them. Independent generation changes invalidate affected
associations. Matching energy alone never proves historical provenance or attempt
ownership. Unsupported/ambiguous associations remain qualified observations.

With no native OUTCAR context, complete source-local rows can have navigable
History entries and be promoted when native evidence arrives. If OUTCAR already
has an earlier evaluation and a stream begins an entirely unassociated later
evaluation, its latest values remain in the card, Details and Copy; navigable
History waits for that native context. Retention stays bounded, and no guessed
outer-evaluation identity is assigned.

Effective current-segment OUTCAR settings take precedence. A separately declared
current INCAR supplies provisional input evidence only; differences from effective
settings remain explicit. No historical launch-input linkage or default threshold
is invented. EDIFF applies to supported |dE| and |d eps|, never rms or rms(c).
EDIFF=0 is not ordinary threshold convergence. Negative EDIFFG is a conditional
force criterion only within the supported relaxation/constraint contract;
positive EDIFFG is ionic-energy based, and static force targets are N/A. Whole-atom
all/free/fixed scopes remain distinct; partial-direction flags are not Cartesian
force masks.

## Frozen handoff snapshots and copying

**Y Copy** captures the selected immutable job/attempt in a read-only preview.
Summary is the default; Diagnostics and JSON expose the same captured evidence at
more detail. The payload stays fixed while new observation arrives. **Refresh**
explicitly recaptures the same attempt from the client cache; it cannot retarget
another job or reparse scientific files. Names and paths are not dashboard-truncated.

```sh
cmw jobs progress J1.1 --snapshot summary
cmw jobs progress J1.1 --snapshot diagnostics --redact-paths
cmw jobs progress J1.1 --snapshot json
# The same explicit --source ROLE[@STAGE]=ABSOLUTE_PATH options also apply.
```

These CLI commands perform one ordinary bounded observation, then serialize it;
History remains a separate view. Core observation and snapshot serialization need
neither Textual nor Rich. Captures include per-component timestamps, execution
scope, source verification, settings origin, data age and scientific acceptance
**not assessed**. A captured client snapshot is not an atomic filesystem/kernel
snapshot. Default exports exclude argv, environment variables, coordinates and raw
log tails. Diagnostics can contain sensitive paths: review the preview and use
path redaction before sharing. No export file is created automatically.

Scientific summaries and diagnostic text support selection. When reliable native
selection is unavailable, Details/History/Logs provide a frozen read-only text
view. Ctrl+C with a real selection copies and consumes the event, including on
clipboard failure; without a selection the existing screen-specific back/detach
behavior remains. Q and process-control letters stay literal in text editors.
Operational shortcuts are disabled while native summary text is selected;
Y Copy and main-screen Q Detach remain available.
Dragging over the Files cell is not an open-folder click.

The bounded clipboard backend reports local-backend acceptance, an OSC52 request,
or unavailable/failed status separately. A verified direct local macOS desktop can
use `/usr/bin/pbcopy` with stdin; remote contexts cannot silently use host pbcopy.
OSC52 delivery depends on terminal support and is only reported as requested.
The selectable preview remains available if copying fails. Automated checks use
stub backends; native clipboard contents are never read or overwritten by tests.

## Bounded reading, lifecycle and freshness

Each regular-file reader keeps its descriptor, byte offset and partial line. One
poll reads at most 64 KiB of new data plus bounded prefix/tail sentinels (at most
256 bytes). Unchanged caught-up polls perform no data reads. Complete newline
records handle arbitrary byte chunks and split UTF-8; incomplete trailing lines
or force/energy blocks remain pending. Oversized/malformed lines have explicit
health diagnostics and bounded storage (16 KiB maximum source line). Nonregular
files are refused. Each binding has at most eight declared sources.

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

Progress durations use `HH:MM:SS` with unbounded hours: `75 s` becomes `00:01:15`,
`6444 s` becomes `01:47:24`, and `90061 s` becomes `25:01:01`. Supported fractional
precision remains fractional; unknown or coarser-than-second source resolution,
or additional display rounding, receives `~` rather than implying exact seconds.
Unavailable or invalid values never become a negative duration or zero. Native
wall and native CPU labels remain distinct. Observer values retain their observer,
interval and completeness qualification; they are not execution-duration estimates.
The original numeric value, unit, clock/source, printed resolution, context and
evidence remain in Details, History details and JSON. Formatting does not change
native association, accumulate CPU into wall time, read extra history or introduce
another polling service.

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
and retains that stage in the descriptor. Stage-aware plans use distinct
`ROLE:STAGE` roles for multiple stages; non-stage-aware plans retain the native
role and accept only one stage. Repeat stage-aware sources in known workflow
order; `--stage` remains a history filter. Plain `ROLE=/absolute/file` is
unchanged. These CLI declarations are invocation-local observation associations,
never edits to the queue or scientific provenance.

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
