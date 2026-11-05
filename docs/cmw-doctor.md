# Runtime and capability doctor

`cmw doctor` observes the code loaded by the selected interpreter. It does not
initialize a queue, contact configured nodes, inspect research directories, start
engines, run runner scripts, install software or repair anything. Default inspection
writes nothing. Use the qualified `-I -B` launcher to audit the deployed snapshot.

```console
cmw doctor --local --json
cmw doctor --capability jobs-ui
cmw doctor --state /explicit/private/state --job J1.1 --json
cmw doctor --node mini --queue research --nodes-config /explicit/frontend/config --job J1.1 --json
```

Local jobs require an explicit `--state`. A node selector connects only to the
selected configured endpoint; remote files are read there. Node/queue/job/attempt
identities are retained. `--capability` selects `jobs`, `jobs-ui`, `nodes`,
`progress:VASP`, `progress:ORCA`, `progress:MULTIWFN`, `thermal` or `vasp-runner`.
Capability availability means implementation presence, not execution readiness,
configured engine compatibility, complete native grammar or scientific acceptance.
The ordinary managed-runner compatibility check remains `cmw vasp check-runner`;
doctor does not invoke or replace it. Thermal provider execution is not probed.
Missing optional UI/thermal capability does not block unrelated core diagnosis.

Exit 0 means the captured report contains no ERROR finding, not calculation
readiness. Exit 1 means at least one ERROR finding. Invalid CLI/export requests
return 2. Every finding separates its code, domain, scope, severity, evidence,
affected capability and next safe read-only check. No suggested action is executed.

## Observer bytecode policy

The local and selected endpoint runtime sections show the interpreter's startup
settings (`sys.flags.isolated`, `sys.flags.dont_write_bytecode`) and its current
`sys.dont_write_bytecode`. Importing CMW can enable the current setting after
startup. Doctor separately reads `sys.orig_argv` to report only explicit `-I`
and `-B` switches before the interpreter's entry point. These switches are
operator diagnostics and grant no runtime trust. The selected node
report pairs its registered SSH executable path with the switches observed in
the resulting interpreter. A local registration runs in the caller. A failed
connection or an older endpoint can leave launch flags unknown. Doctor never
executes or reads the registered wrapper to infer its flags.

Doctor checks effective write access as the package-root owner to existing
bytecode and its directories and to source directories where caches could be
created. Insufficient evidence or a different observing account gives
`UNKNOWN`. This bounded read-only check, the launch flags and isolation are
advisory. A warning does not change queue readiness or runtime trust.
For a sticky refusal, follow the
[bytecode cache recovery walkthrough](runtime-observation-policy.md#bytecode-cache-recovery).

## Which runtime is being described?

The implementation fingerprint is the existing `nodes.identity.runtime_identity()`
contract, with its exact loaded package bytes, interpreter and dependency versions.
Package origins, distribution metadata and optional source checkout identity are
reported separately. An unrelated Git HEAD is never attributed to an installed
wheel. Dirty/source-less installations have no asserted build commit. This delivery
does not embed a new build-source field or change the strict runtime wire model.
Reproducible wheel bytes and the existing implementation identity are checked in
campaign acceptance; an external report records the corresponding source commit.

`cmw --version` now says **CMW distribution VERSION** (or `uninstalled`). Previously
it printed `cmw-attempt-cleanup/1`. The cleanup-tool version remains unchanged and
appears separately alongside node protocol 1, doctor report schema 1, supported
Jobs schemas 1/2/3/4 and explicitly bounded scientific grammar descriptions.

For a selected node, caller and endpoint runtimes are distinct. An existing worker's
runtime remains UNKNOWN unless established by appropriate producer evidence. The
current queue format records no independently verified CMW worker-import identity;
a submission runtime pin is an expectation, not proof of current worker imports.
A differing pin is reported without upgrading or interrupting the worker.

Selected-node doctor uses its own explicit `doctor.inspect` operation. Older
endpoints reject it before backend initialization; absence is not interpreted as
compatibility. The runtime identity codec is unchanged. New report schema, owner,
attempt, envelope and finding checks fail closed. Doctor readiness checks only
selected queue/host/runtime identity; unrelated storage/runner/thermal checks are
not performed. Other operations retain their existing full checks.

## Bounded observations and qualification

Selected-job inspection reuses automatic source resolution, the shared reader and
adapter, and frozen snapshot serialization. It performs one poll: at most eight
sources and the existing 64 KiB per-source incremental read budget. Pending files,
binding conflicts, unsupported dialects, parsed metric/projection gaps and incomplete
coverage remain separate. No native lines, metric values, coordinates, argv,
environment, complete database or raw log tail enters the default doctor report.
This is not a full replay or result validation. Controller facts are recorded
metadata; process ownership and execution health are not newly probed.

Read-only SQLite access uses the existing rollback-journal safety pattern under
a read lock. Unsupported WAL/journal formats are refused without creating sidecars.
No migrations, permission changes, source activation, enrollment or reconciliation
are performed. If safe evidence cannot be obtained, the report says unavailable.

Remote receipt time never refreshes source evidence time. After a failed connection,
previously verified envelope information is explicitly cached/LAST_REPORTED; current
endpoint and worker imports are not inferred. The Doctor route is **Details → D**
in both local and node dashboards. Inspection is bounded and asynchronous; closing
the modal does not control execution. The compact table, Machine line and primary
shortcuts are unchanged.

## Private support capsule

```console
cmw doctor --state /explicit/private/state --job J1.1 --export /explicit/private/new-cmw-capsule
```

Export is explicit. Select a new directory below your Desktop or a Scratch directory;
all parent directories must already exist. The exporter rejects existing destinations,
traversal and symlink ancestors. The new directory is 0700, files 0600. Maximums are
256 KiB per file and 1 MiB total. `doctor.json` contains a frozen allowlisted projection;
`manifest.json` records its exact byte count/SHA-256, limits and exclusions. The
manifest explicitly excludes itself from self-hashing. No automatic upload occurs.

Paths, arbitrary identities and free text receive consistent per-bundle aliases;
the alias-to-private-value map is not exported. Numeric immutable job selectors are
also aliased. Runtime hashes, timestamps, coverage, known finding codes and trusted
read-only guidance remain useful. Unknown nested fields are dropped. Arbitrary text
is not made safe merely by resembling a diagnostic field.

No environment, SSH material, credentials, full argv, logs, coordinates, POTCAR,
GBW/WAVECAR/CHGCAR, complete databases or archive contents are collected. Report
paths are never opened by export, and nested archives are never extracted. **Raw
excerpt selection/import is unsupported** in this delivery; it cannot bypass the
allowlist. Allowlists/redaction are not proof that arbitrary user-selected text is
secret-free. A capsule without native logs is not a complete replay corpus.

L1 contract/unit, L2 installed/local-endpoint, and L3 isolated transport/UI results
are recorded in the private Desktop campaign acceptance report. Real scientific
engine execution (L4) and a physical second-node qualification (L5) are separate,
unperformed gates. Do not switch a live runtime merely because a new doctor exists.
