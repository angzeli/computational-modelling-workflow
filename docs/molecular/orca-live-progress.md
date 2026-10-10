# ORCA live progress and history

This is a read-only adapter for the [shared progress contract](../cmw-jobs-progress.md).
It observes printed evidence without launching ORCA or changing molecular status,
finalization, Jobs state, scheduling, process ownership or scientific records.
Files and raw logs remain available for unsupported stages. CP2K is unimplemented.

## Qualified grammar

The initial scope is ordinary ground-state SCF and geometry optimization using
ORCA 6.1.x output forms demonstrated by the 6.1 documentation and a completed
local 6.1.1 constrained optimization. The adapter requires both a supported banner
and recognized structural grammar. A banner alone does not establish arbitrary
ORCA version, algorithm or workflow support. Private coordinates and output dumps
are not committed; tests use fictional numerical data.

Supported electronic tables have the exact normalized column descriptors:

```text
Iteration Energy (Eh) Delta-E RMSDP MaxDP DIISErr Damp Time(sec)
Iteration Energy (Eh) Delta-E RMSDP MaxDP MaxGrad Time(sec)
```

The first is DIIS; the second is SOSCF. Algorithm transitions and repeated/reset
iteration labels create distinct comparison contexts. Unknown headers have no
positional fallback. TRAH iteration progress is diagnostic/unsupported; it is not
forced into either table. Post-SCF `Last ... Tolerance:` summaries use distinct
semantic IDs and are not fabricated live iterations.

Geometry cycles use the native `GEOMETRY OPTIMIZATION CYCLE` boundary and labelled
`Geometry convergence` tables. The first table may omit energy change. A partial
row or table never becomes zero. Native SCF/optimization convergence and failure
markers are retained separately from numeric threshold relations and program
termination. A process exit code, quiet output or low gradient cannot establish
scientific validity.

The scientific basis is the official [SCF convergence contract](https://www.faccts.de/docs/orca/6.1/manual/contents/essentialelements/scf.html),
[geometry definitions](https://www.faccts.de/docs/orca/6.1/manual/contents/structurereactivity/optimizations.html)
and [labelled geometry output tutorial](https://www.faccts.de/docs/orca/6.1/tutorials/prop/geoopt.html).
Ordinary DIIS/SOSCF header forms also appear in the official
[DeltaSCF examples](https://www.faccts.de/docs/orca/6.1/manual/contents/spectroscopyproperties/deltascf.html);
that does not extend this adapter to specialized DeltaSCF/root-following semantics.

## Metric definitions, criteria and current context

| Native observation | Unit | Meaning retained |
| --- | --- | --- |
| Geometry energy change | Eh | Magnitude, with original signed printed value retained |
| RMS gradient | Eh/bohr | Native optimization-table RMS gradient |
| MAX gradient | Eh/bohr | Maximum gradient component; not an atomic force-vector norm |
| RMS step / MAX step | bohr | Native optimization-table displacement summaries |
| SCF energy / energy change | Eh | Iterative electronic energy / change magnitude |
| RMSDP / MaxDP | unspecified | Native density-matrix changes; header supplies no unit |
| DIISErr | unspecified | DIIS diagnostic, not VASP rms(c) |
| SOSCF MaxGrad | unspecified | Orbital-gradient component, distinct from geometry gradient |

Gradient and displacement scope is the native optimization table, including its
reported constraint treatment. CMW does not reconstruct constrained forces.

Geometry thresholds come from the current run's table. SCF tolerances and budget
come from recognized effective-output settings. Named presets such as TightOpt
or TightSCF never supply hardcoded effective values. A metric can have no target.
The native YES/NO flag and the numeric relation are both preserved; printed
resolution can make the numeric boundary uncertain or disagree with the flag.

SCF convergence-check mode is shown when printed. Mode 0 includes native
exceptions, mode 1 permits one criterion, and mode 2 uses energy-based checks;
the adapter does not synthesize an AND of all displayed tolerances. Unknown or
missing mode remains explicit, and a tolerance alone is not an established global
stopping rule.

The card prefers maximum gradient component, RMS gradient and the current SCF
diagnostic. All other supported metrics, native flags, criteria and exact source
references remain available in Details/history/JSON. During a new SCF evaluation,
carried geometry observations retain their earlier cycle labels; no current
geometry gradient is invented. Latest/previous pairs require compatible units,
definitions, criteria, source generation, stage, segment and algorithm context.
Native cycles are not silently promoted to accepted optimizer steps or trial
identities absent explicit evidence.

## Exact sources and stages

The registered source role is `orca_output`. `run_orca.sh --output` declares an
exact text file, separately from other artifacts such as XYZ or GBW. Jobs stdout
and cwd are not fallback output associations. No recursive glob or newest-file
selection is used.

The existing workflow-state resolver accepts checked exact records for OPT,
FREQ and SP in recorded order. Each source carries its own stage. A later stage
clears active criteria/metrics while earlier evidence remains in stage-filtered
history. Recognized new-run and supported stage boundaries also separate contexts
inside a single output. A prior native convergence marker never certifies a later
incomplete run. Ambiguous compound steps remain unsupported.

The ordinary workflow wrapper's root directory does not identify its hashed
workflow-state file, so that argv-only route still needs explicit sources. External
Scratch redirects native output to an execution path; its copyback destination is
not assumed to be a live source. Declare the exact execution source explicitly.

```sh
cmw jobs progress J1.1 --engine ORCA --source orca_output=/absolute/stage.out --json
cmw jobs progress J1.1 --engine ORCA --history --json \
  --source orca_output@OPT=/absolute/opt.out \
  --source orca_output@FREQ=/absolute/freq.out \
  --source orca_output@SP=/absolute/sp.out
cmw jobs progress J1.1 --history --stage OPT --parent '*' --json
```

Repeat explicit sources in known workflow order. These declarations replace the
source plan for that invocation, do not edit Jobs or provenance, and do not prove
historical scientific lineage. Conflicting recorded stage/file associations are
refused. Source replacement/truncation uses the shared reader's generation and
cursor invalidation; no earlier generation is silently spliced into a new run.

In watch, G opens history; Enter drills down; D shows full evidence; Backspace
returns; [ and ] page; S/V select stage/segment. Existing F/Files, N/Note, P/Dispatch,
O/Order and detach behavior retain their meanings. No ORCA-specific widget or
console is introduced.

## Timing and unavailable periods

The native `TOTAL RUN TIME` days/hours/minutes/seconds/msec footer is a run-level
wall-time total covering the native run, including any internal stage transitions. It remains cumulative and is never divided by cycle count or
differenced to manufacture cycle durations. Resets/new runs are separate segments.

The demonstrated SCF `Time(sec)` column does not independently establish the
clock and interval scope required by the shared timing contract. Its printed token
is retained diagnostically; it is not relabelled wall time or CPU time. Missing
per-cycle native wall duration remains unavailable.

Qualified same-session monotonic observer intervals describe observation only.
Unknown start time and buffered output prevent treating them as complete execution
durations. A batch of first-seen rows gets no fake zero-length cycles. Reopening
reconstructs native evidence; unrecorded observer-only timing stays unavailable.
Clock reversal or identity change is invalid, not zero. No mtime, utilization,
interpolation or job-elapsed shortcut recovers missing time.

## Deferred scientific modes

Specialized root-following/excited-state optimization, NEB/IRC/transition states,
scans, unsupported compound steps, MD, correlated-method iteration loops and
unknown dialects remain outside this progress scope. FREQ stage recognition is
useful, but displacement progress without established totals is not implemented.
Starting FREQ or SP clears the OPT gradient target from the active card.

These boundaries do not change existing ORCA launch or scientific-finalization
support. See the shared [adapter extension checklist](../cmw-jobs-progress.md#adding-a-bounded-engine-adapter)
for future engines and the fictional three-level cross-engine acceptance fixture.


## Adapter work bounds

The shared service retains at most eight contexts, eight sources per context,
two inflight context operations and one operation per context. Each regular-file
read is bounded to 64 KiB plus at most 256 sentinel bytes. A history slice can do
one live and one replay read budget per source; cached unchanged pages avoid
source-content rereads. The console retains its single worker and normal four-
second polling cadence after catch-up.

ORCA processes at most 2,048 queued complete lines per update, with one bounded
reader batch queued per source and a 4,096-pending-history-entry backpressure
threshold. It retains one current evaluation, the last SCF row, small bounded
metric maps, 64 recent events, 32 diagnostics, 128 echoed tokens and at most 32
current-evaluation annotations. It does not retain molecular coordinate arrays.
The shared history cache retains 256 recent entries and four pages per context,
with at most 200 entries per page; older native history reconstructs on demand.

If an earlier declared stage grows after a later stage begins, or one file of a
multi-source binding is replaced, the adapter refuses to silently combine the
streams. Resolve/reopen a fresh explicit binding. Missing future output remains
missing evidence and never proves stage completion. Outstanding I/O is released
cooperatively after it returns; closing a view is not an instantaneous filesystem
cancellation promise.
