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
The adapter must still qualify actual source evidence. Several declared workflow
stages require an explicit independent run context rather than guessing a stage.

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
`SourcePlan` entries map literal filenames to declared location roles. There is no
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
evidence, not a full history API. The fictional three-level geometry → outer →
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

Full history/timing presentation, ORCA parsing and CP2K parsing are deferred.
