# Two-node cutover runbook and MacBook sandbox rehearsal

Status: **draft; production cutover is not authorized by this document**. Execute
the sandbox steps only in a separately authorized rehearsal. The Mini steps are
deferred until its research calculation finishes and its remaining execution
evidence has been reviewed. A completed calculation and a retired ownership
anchor are separate conditions.

This procedure uses these immutable candidates:

| Component | Commit | Evidence boundary |
|---|---|---|
| CMW | `8d4cfc3f71ee51696f4783c579b256f05d2e16a0` | Includes PR #39 retirement. Fresh frozen-installation retirement rehearsal passed (record below); Mini qualification and deployed-revision parity remain required. |
| VASP port runner | `4fd6fa7e4df1032a97703ce18fc6c033273d46ed` | D31 runner; only the CMW qualification pin differs from R (`3c43819`). Its `scripts/launch.py` blob is `a534d39a522ebe6b9be0a6516d79f87b341fcafd`. |

The [runner qualification record](../architecture/vasp-runner-ownership-plan.md)
records the synthetic CI qualification of this exact pairing. The
[deployed-runtime parity procedure](../architecture/deployed-runtime-parity.md)
records 17 known CMW revisions and the unresolved inventory evidence. Neither
document establishes that the operations below have run. Record actual results
in a dated private rehearsal report; do not overwrite expected results with
claims inferred from CI.

Run command blocks in a dedicated Bash session with `set -euo pipefail`; replace
and verify each placeholder before running its block. An error stops the current
step. Do not continue into later mutation steps after a failed prerequisite.

## 1. Preconditions and decisions that stop cutover

**Precondition:** read D01–D03, D30–D34 in the
[multi-host specification](../architecture/multi-host-nodes.md), the
[J4.1 incident](../incidents/2026-11-j4.1-clock-step.md), and the
[shared unavailable-evidence rule](../periodic/vasp-result-evidence.md#unavailable-evidence).

**Command/evidence:** inspect the explicitly selected original queue with its
existing qualified runtime, after separate authorization to access that machine:

```sh
"$ORIGINAL_CMW" jobs --state "$ORIGINAL_STATE" status --json
"$ORIGINAL_CMW" jobs --state "$ORIGINAL_STATE" show "$ORIGINAL_JOB" --json
ownership_code=0
"$CANDIDATE_CMW" jobs --state "$ORIGINAL_STATE" ownership "$ORIGINAL_JOB" --role group --json || ownership_code=$?
case "$ownership_code" in
  0) printf 'Anchor remains VERIFIED; retirement precondition is not met\n' ;;
  2) printf 'Anchor is confirmed GONE\n' ;;
  75) printf 'Anchor is UNVERIFIABLE; stop\n' >&2; exit 75 ;;
  *) exit "$ownership_code" ;;
esac
"$CANDIDATE_CMW" jobs --state "$ORIGINAL_STATE" reconcile "$ORIGINAL_JOB" --preview --json
```

**Expected:** no `Starting` or `Run` record; no live scientific calculation,
unresolved worker, completion hold or unresolved ownership anchor. A displayed
`Unknown` is not evidence of absence. Ownership exits are VERIFIED `0`, GONE `2`
and UNVERIFIABLE `75`; unavailable evidence never authorizes retirement.

**Stop/rollback:** these are diagnosis commands, not recovery. Preserve the old
queue, configuration, runtime, storage and all research evidence. Do not change
SQLite, mark a job terminal by hand, or run `reconcile --apply`: apply is
unimplemented and always refuses.

The Mini node ID is `702779636fc14335bcf71c1395549f43`; the original queue ID is
`2b975d09e4104933b7c1c146538fbc6b`. Existing D01–D03 require their preservation,
including the original storage UUID. **D01 remains unchanged.** The following
implementation limits stop production cutover:

* `nodes enroll` accepts an existing node ID but creates new storage IDs and
  refuses an existing `node.json`. It has no alias/runtime/profile migration
  command. An existing queue can retain its UUID only at its unchanged root and
  database identity.
* An enrolled queue binds its absolute path and root/database device and inode.
  Moving, renaming or copying it is not identity-preserving archival.
* `nodes connect` refuses changed evidence under an existing alias and refuses
  the same node under a second alias. It is not an update command.
* `cancel` refuses `Unknown`. `stop` stops the controller and dispatch; it does
  not drain workers or retire an anchor.

**Mini J4.1 step: supported retirement (PR #39).** Use a qualified candidate
containing PR #39 on the original queue in place. Preserve all node, queue,
storage, job and attempt identities; no archive-and-reenroll alternative or
manual database edit is authorized. See the [retirement contract](../cmw-jobs-retirement.md).
The following is a procedure for the separately authorized Mini cutover window,
not evidence that it has been performed.

**Before the window — v1 limits:** J4.1's recorded identities are v1, including
worker PID `90837` (reported dead on 3 November) and anchor PID `90838`.
Keep network time on and do not reboot the Mini. Read the complete recorded
identity list from the original job/attempt evidence, then inspect those PIDs
read-only, for example:

```sh
ps -p 90837,90838 -o pid,lstart,command
```

Repeat for every other recorded PID. An absent PID makes `ps` return nonzero;
inspect and record that result explicitly rather than treating it as proof of
all retirement preconditions. Retirement refuses with exit **75** if a reboot
or clock step has changed the recorded v1 boot timestamp, or if any recorded
PID has been reused with a different birth time. In particular, check for reuse
of worker `90837`. Wall-clock dates alone cannot override the tri-state reader.
Network time being enabled does not repair an existing mismatch.

1. Confirm the original J4.1 calculation has ended and independently review its
   outputs: the OUTCAR completion/timing footer and a complete, parseable
   `vasprun.xml` with its closing document element. Preserve this evidence and
   check the scientific convergence separately; completeness is not convergence.
   Stop the original controller through its supported command and confirm its
   ownership lock is free, keeping dispatch paused.
2. Match the leftover anchor to the **original recorded identity**. Require the
   candidate's `ownership ... --role group --json` result to be VERIFIED (exit
   0), and inspect `ps -p 90838 -o pid,lstart,command,pgid` and obtain the SID
   read-only with the qualified Python interpreter’s `os.getsid(90838)`: the expected
   process is a bash command started on **2 November 2026**, with SID and PGID
   **90838**. Verify the complete start identity, not merely this date or PID.
   Establish that no computation or receipt writer remains. Revalidate the
   original identity immediately before the separately authorized signal, then
   send **SIGKILL to PID 90838 only** (positive PID, never its process group).
   The lost anonymous control pipe cannot be recreated by upgrading CMW.
   If the identity is GONE, do not signal its current PID; if UNVERIFIABLE or
   inconsistent, stop. After signalling, wait for reaping and require ownership
   exit 2 / GONE. A zombie or unreadable probe is insufficient.
3. Retire promptly after ending the anchor, using the verified original state
   root and immutable J4.1 job identifier:

   ```sh
   "$CANDIDATE_CMW" jobs --state "$ORIGINAL_STATE" retire "$ORIGINAL_JOB" \
     --reason "Cutover: outputs reviewed independently; recorded execution processes confirmed gone" --json
   ```

   Require exit 0 and RETIRED (or an already-retired matching attempt). Inspect
   every reported identity observation for GONE and the empty recorded
   session/group evidence. The disposition is administrative Fail with execution
   outcome unknown; it does not finalize or certify the scientific result.
4. Re-read `show` and `status --json` on that original queue. Confirm the
   retirement evidence is retained, J4.1 is terminal, and it no longer supplies
   an Unknown/ownership admission block. Check other attempts and admission
   reasons as well. Dispatch deliberately remains paused: retirement does not
   enable it. Only after all other cutover gates pass may the operator explicitly
   start the qualified controller or resume dispatch and confirm admission is
   unblocked. Do not dispatch a research job merely to test this condition.

**Stop:** if retirement refuses for any reason, stop the Mini cutover and report
its JSON evidence and exit code. Preserve the original queue and outputs; do not
edit the DB, rewrite identities, kill a reused PID, reboot as a workaround or
force a terminal result. The v1 earlier-boot exception remains proposal-only.

## 2. Create a private sandbox and record inputs

The candidate below includes retirement. The recorded earlier full rehearsal used
`66a372a`; the refresh record below covers the new runtime surface and the frozen
installation. It does not replace Mini-specific qualification before live cutover.

**Precondition:** choose a new date/name; it must not already exist. The source
repositories must contain each pinned commit. Archive those exact Git objects;
the source checkout's HEAD or unrelated working changes do not select the
snapshot. The qualified Python path is absolute
and belongs to the selected installation; preserve its virtual-environment
symlink spelling. Licensed input and binary sources are supplied by the operator.
Do not read production calculation directories or copy a whole run tree.

```sh
set -euo pipefail
export CMW_COMMIT=8d4cfc3f71ee51696f4783c579b256f05d2e16a0
export RUNNER_COMMIT=4fd6fa7e4df1032a97703ce18fc6c033273d46ed
export REHEARSAL_ROOT="$HOME/.local/share/cmw-sandbox/cutover-rehearsal-YYYYMMDD"
export CMW_SOURCE=/absolute/cmw-repository
export RUNNER_SOURCE=/absolute/runner-repository
export QUALIFIED_PYTHON=/absolute/qualified/python3.14
test ! -e "$REHEARSAL_ROOT"
umask 077
mkdir -m 700 "$REHEARSAL_ROOT"
mkdir -m 700 "$REHEARSAL_ROOT/snapshots" "$REHEARSAL_ROOT/evidence" \
  "$REHEARSAL_ROOT/node" "$REHEARSAL_ROOT/registry" \
  "$REHEARSAL_ROOT/state" "$REHEARSAL_ROOT/storage" "$REHEARSAL_ROOT/runs"
test "$(git -C "$CMW_SOURCE" rev-parse --verify "$CMW_COMMIT^{commit}")" = "$CMW_COMMIT"
test "$(git -C "$RUNNER_SOURCE" rev-parse --verify "$RUNNER_COMMIT^{commit}")" = "$RUNNER_COMMIT"
```

**Expected:** all writable rehearsal material is beneath this new root. No
production registry, launcher, runtime, queue or runner is modified. Private
reports may contain actual local paths; public reports use node aliases and
placeholders and contain no credentials, host addresses or licensed content.

**Rollback:** before any process exists, retain the failed preparation evidence
and remove only this newly created sandbox if requested. Never clean a directory
that existed before this step.

## 3. Build immutable candidate snapshots

**Precondition:** the exact commits above are verified. Installation dependencies
come from the existing lock, without `--upgrade`. Supply qualified runner
binaries and their existing provenance separately; a Git source archive does
not contain or qualify licensed binaries.

```sh
export CMW_SNAPSHOT="$REHEARSAL_ROOT/snapshots/cmw-$CMW_COMMIT"
export RUNNER_SNAPSHOT="$REHEARSAL_ROOT/snapshots/runner-$RUNNER_COMMIT"
mkdir -m 700 "$CMW_SNAPSHOT" "$RUNNER_SNAPSHOT"
git -C "$CMW_SOURCE" archive "$CMW_COMMIT" | tar -xf - -C "$CMW_SNAPSHOT"
git -C "$RUNNER_SOURCE" archive "$RUNNER_COMMIT" | tar -xf - -C "$RUNNER_SNAPSHOT"
printf '%s\n' "$CMW_COMMIT" > "$CMW_SNAPSHOT/source-commit.txt"
printf '%s\n' "$RUNNER_COMMIT" > "$RUNNER_SNAPSHOT/source-commit.txt"
UV_CACHE_DIR="$REHEARSAL_ROOT/cache" UV_PROJECT_ENVIRONMENT="$CMW_SNAPSHOT/.venv" \
  UV_PYTHON_DOWNLOADS=never uv sync --project "$CMW_SNAPSHOT" --python "$QUALIFIED_PYTHON" \
  --frozen --no-editable --extra jobs
export CMW_MANAGED_PYTHON="$CMW_SNAPSHOT/.venv/bin/python"
export CANDIDATE_CMW="$CMW_SNAPSHOT/cmw-endpoint"
"$CMW_MANAGED_PYTHON" -I -B - "$CANDIDATE_CMW" "$CMW_MANAGED_PYTHON" <<'PY'
from pathlib import Path
import os, shlex, sys
target, interpreter = Path(sys.argv[1]), sys.argv[2]
with target.open('x') as handle:
    handle.write('#!/bin/sh\nexec ' + shlex.quote(interpreter) + ' -I -B -m cmw.cli "$@"\n')
os.chmod(target, 0o700)
PY
"$CANDIDATE_CMW" --help
"$CMW_MANAGED_PYTHON" -I -B -c \
  'import cmw, sys; print(sys.executable); print(cmw.__file__); print(sys.flags.isolated, sys.flags.dont_write_bytecode)'
```

Populate only the approved runner binary/configuration files in the new runner
snapshot. `QUALIFIED_VASP_STD` names the existing qualified binary, and
`QUALIFIED_TOOLCHAIN_ENV` optionally names the already qualified local override.
Do not copy POTCAR into a snapshot or alter a production runner.

```sh
mkdir -p "$RUNNER_SNAPSHOT/macos-arm64/bin" "$RUNNER_SNAPSHOT/private"
install -m 700 "$QUALIFIED_VASP_STD" "$RUNNER_SNAPSHOT/macos-arm64/bin/vasp_std"
printf 'synthetic\n' > "$RUNNER_SNAPSHOT/private/mpi-mode.txt"
if test -n "${QUALIFIED_TOOLCHAIN_ENV:-}"; then
  install -m 600 "$QUALIFIED_TOOLCHAIN_ENV" "$RUNNER_SNAPSHOT/private/toolchain.env"
fi
"$CMW_MANAGED_PYTHON" -I -B - "$RUNNER_SNAPSHOT" <<'PY'
import hashlib, json, sys
from pathlib import Path
runner = Path(sys.argv[1])
binary = runner / 'macos-arm64/bin/vasp_std'
expected = next(line.split()[0] for line in
    (runner / 'provenance/binary-sha256.txt').read_text().splitlines()
    if line.endswith('vasp_std'))
actual = hashlib.sha256(binary.read_bytes()).hexdigest()
assert actual == expected, 'Selected VASP binary differs from runner provenance'
(runner / 'selected-binaries.json').write_text(json.dumps({'vasp_std': actual}) + '\n')
PY
"$CMW_MANAGED_PYTHON" -I -B "$RUNNER_SNAPSHOT/scripts/audit-binary.py" \
  "$RUNNER_SNAPSHOT/macos-arm64/bin/vasp_std" > "$REHEARSAL_ROOT/evidence/binary-audit.txt"
```

Retain the audit's executable/dependency identities. The MPI mode here is the
MacBook rehearsal mode; select and qualify the Mini's own mode at cutover.

Check the exact selected runner interface using `cmw vasp check-runner` and the
approved argv. Verify, in a subshell, that sourcing the snapshot environment
preserves the chosen interpreter:

```sh
export RUNNER="$RUNNER_SNAPSHOT/scripts/run-vasp.sh"
/bin/bash -c 'set -eu; expected=$CMW_MANAGED_PYTHON; . "$1/scripts/environment.sh"; test "$CMW_MANAGED_PYTHON" = "$expected"' \
  rehearsal "$RUNNER_SNAPSHOT"
```

**Expected:** package origin is the candidate installation; startup flags are
`1 1`; the runner uses the explicit selected interpreter. The existing wrapper
may contain `${CMW_MANAGED_PYTHON:-python3}`, but the fallback is never used in
qualification. A schema-2 profile pin alone does not enforce this launch path;
automatic enforcement remains [issue #36](https://github.com/angzeli/computational-modelling-workflow/issues/36).

After the build/interface preflights and before enrollment or runtime
qualification, freeze the two snapshots:

```sh
"$CMW_MANAGED_PYTHON" -I -B - "$CMW_SNAPSHOT" "$RUNNER_SNAPSHOT" <<'PY'
import os, stat, sys
for root in sys.argv[1:]:
    for directory, subdirs, files in os.walk(root, topdown=False, followlinks=False):
        for path in [*(os.path.join(directory, name) for name in files + subdirs), directory]:
            mode = os.lstat(path).st_mode
            if stat.S_ISREG(mode) or stat.S_ISDIR(mode):
                os.chmod(path, stat.S_IMODE(mode) & ~0o222, follow_symlinks=False)
PY
```

This deliberately leaves symlink targets outside the snapshots unchanged.

The required order is: install all files/dependencies and any qualified bytecode;
finish runner configuration and binary manifests and their interface preflights;
apply its final ownership, permissions, ACLs and extended attributes; then start
a **fresh** `-I -B` process to enroll. Only afterwards construct the registry from
the returned enrollment, open a fresh backend and check capabilities, status and
Doctor. There must be no `chmod`, installer, compilation or metadata repair
between enrollment and those checks.

The semantic runtime fingerprint hashes package content, Python/version and
dependency versions; it excludes package/executable locators and filesystem
security metadata (`nodes/identity.py:191–210`). Enrollment separately persists
the full original runtime observation (`identity.py:535,563–565`), including
member and directory identity/security and interpreter evidence
(`runtime_observation.py:131–148,400–405`). Matching the fingerprint alone is
therefore insufficient. Material mode changes refuse against that original
observation (`runtime_observation.py:607–629,768–792`); the observer stays tainted
until replaced (`:651–662`). Profile interpreter pins also include their full
epoch (`runner_profiles.py:24–35`).

The sandbox audit demonstrated this distinction: removing owner-write from
`cli.py` after capturing a baseline left the runtime fingerprint unchanged but
changed the observation and refused with `security_metadata_changed`. Applying
the final no-write permissions **before** enrollment then produced
`ready: true` and `runtime_ready: true`; Doctor and three subsequent status calls
passed. This is the order to retain for deployment. The separate post-cutover
ctime observation finding is tracked in
[issue #38](https://github.com/angzeli/computational-modelling-workflow/issues/38);
it is not permission to relax metadata verification or alter an enrolled tree.

**Rollback:** reject the candidate on any changed origin, missing dependency,
manifest mismatch or environment override. Correct only a new, unenrolled
snapshot and repeat its affected qualification. Do not make an enrolled runtime
writable and edit it in place; that invalidates its trust evidence.

## 4. Enroll the sandbox and construct its local registry

**Precondition:** snapshots have their final permissions. Storage exists on the
selected supported local filesystem. This is a fresh sandbox enrollment, not a
recipe for replacing Mini production identities.

```sh
"$CMW_MANAGED_PYTHON" -I -B - <<'PY'
import json, os
from pathlib import Path
root = Path(os.environ['REHEARSAL_ROOT'])
spec = {
    'queues': {'main': str(root / 'state')},
    'storages': {'scratch': str(root / 'storage')},
    'profiles': {
        'command': {'engine': 'Command', 'runner': '/bin/sleep', 'max_cpus': 1},
        'vasp': {'schema': 2, 'engine': 'VASP', 'runner': os.environ['RUNNER'],
                 'runner_interpreter': os.environ['CMW_MANAGED_PYTHON'],
                 'max_cpus': 1, 'max_ranks': 1,
                 'dependencies': [
                     str(Path(os.environ['RUNNER_SNAPSHOT']) / 'macos-arm64/bin/vasp_std'),
                     str(Path(os.environ['RUNNER_SNAPSHOT']) / 'selected-binaries.json')]}
    }
}
with (root / 'node-spec.json').open('x') as handle:
    json.dump(spec, handle, indent=2)
PY
"$CANDIDATE_CMW" nodes enroll --config "$REHEARSAL_ROOT/node" \
  --spec "$REHEARSAL_ROOT/node-spec.json" --json > "$REHEARSAL_ROOT/evidence/enrollment.json"
"$CMW_MANAGED_PYTHON" -I -B - <<'PY'
import json, os
from pathlib import Path
root = Path(os.environ['REHEARSAL_ROOT'])
node = json.loads((root / 'evidence/enrollment.json').read_text())
entry = {
    'transport': 'local', 'config_root': str(root / 'node'),
    'node_id': node['node_id'], 'host_id': node['host_id'],
    'runtime_id': node['runtime']['fingerprint'],
    'queues': {'main': node['queues']['main']['queue_id']},
    'default_queue': 'main', 'read_only': False
}
with (root / 'local-connection.json').open('x') as handle:
    json.dump(entry, handle, indent=2)
PY
"$CANDIDATE_CMW" nodes connect mbp_m3max --config "$REHEARSAL_ROOT/registry" \
  --spec "$REHEARSAL_ROOT/local-connection.json" --json
"$CANDIDATE_CMW" nodes list --config "$REHEARSAL_ROOT/registry" --json
"$CANDIDATE_CMW" nodes check mbp_m3max --config "$REHEARSAL_ROOT/registry" --queue main --json
"$CMW_MANAGED_PYTHON" -I -B - <<'PY'
import json, os
from pathlib import Path
from cmw.nodes.identity import check_node, load_node
root = Path(os.environ['REHEARSAL_ROOT'])
result = check_node(load_node(root / 'node'))
(root / 'evidence/full-readiness.json').write_text(json.dumps(result, indent=2) + '\n')
assert all(result[key] is True for key in
           ('ready', 'identity_ready', 'runtime_ready', 'storage_ready', 'profiles_ready'))
assert result['errors'] == []
PY
```

**Expected:** recorded UUIDs come from enrollment output, not copied display
aliases. Check node/queue/runtime identity and every selected profile/storage's
readiness. The VASP interpreter is reported pinned and verified. Each state
root and registry points exclusively into the sandbox.
`nodes check` alone checks scoped capabilities and does not verify every profile
and storage; the full read-only `check_node` above establishes those checks.

**Rollback:** stop on any refusal; retain the error and configuration. Do not
overwrite `node.json`, change its UUIDs, or repair a registry by hand. For a
fresh rehearsal, start another new sandbox after diagnosing the mismatch.

## 5. Check observation, controller and ownership

**Precondition:** the registry above passes its checks. Confirm the authorized
process observation shows no conflicting calculation; do not open production
queues or calculation files. Keep all test work at
`nice -n 19`, one process at a time. Do not run saturation or full local suites
while a real calculation is running.

```sh
"$CANDIDATE_CMW" doctor --node mbp_m3max --queue main \
  --nodes-config "$REHEARSAL_ROOT/registry" --json
export CMW_MANAGED_PYTHON="$CMW_SNAPSHOT/.venv/bin/python"
export CMW_JOBS_OWN_SESSION=1
export CMW_JOBS_GUARD_RECOVERY_SECONDS=5
nice -n 19 "$CANDIDATE_CMW" jobs --state "$REHEARSAL_ROOT/state" start --json
"$CANDIDATE_CMW" jobs --state "$REHEARSAL_ROOT/state" pause --json
"$CANDIDATE_CMW" jobs --nodes-config "$REHEARSAL_ROOT/registry" \
  --node mbp_m3max --queue main status --json
"$CANDIDATE_CMW" jobs --nodes-config "$REHEARSAL_ROOT/registry" watch --all-nodes
```

Close the portal normally after observing it; closing an observer does not stop
the controller. Status cadence is 5 s; retained status becomes stale after 15 s
inclusive, using the receiving portal's monotonic clock. Disconnect immediately
disables controls and labels data last reported. A reconnect requires a compatible
handshake and new data. Do not stop a production endpoint to test disconnection.

**Expected:** Doctor distinguishes startup flags from the effective no-bytecode
flag, selected identities remain unchanged, and controller status records the
effective guard recovery interval. Any external scientific activity remains
external and blocks admission as designed; do not override the guard to make a
smoke job run.

**Rollback:** pause dispatch and request sandbox controller stop. Diagnose a
refusal using the recorded identities. No blanket `pkill`, inherited PID list,
or production controller restart is authorized.

## 6. Exercise execution and cancellation in the sandbox

**Precondition:** separate authorization permits the selected VASP smoke,
admission is ready, and no VASP or MPI process is running on this machine.
All writable paths are below the sandbox; the approved original licensed
input is the sole read-only input/CWD exception. Use one rank. Parameters describe the explicitly approved
two-atom silicon fixture; they are not defaults for research.

For a benign control test, create a new working directory, register held and
capture the returned immutable attempt ID before release:

```sh
mkdir -m 700 "$REHEARSAL_ROOT/runs/control"
"$CANDIDATE_CMW" jobs --state "$REHEARSAL_ROOT/state" add \
  --name cutover-control --cwd "$REHEARSAL_ROOT/runs/control" --engine Command \
  --cpus 1 --memory-gib 0.1 --hold -- /bin/sleep 30
```

Set `SMOKE_JOB` to the returned display ID and `SMOKE_ATTEMPT` to its immutable
attempt ID after checking its paths. Release/resume do not wait for launch:

```sh
"$CANDIDATE_CMW" jobs --state "$REHEARSAL_ROOT/state" release "$SMOKE_JOB" --json
"$CANDIDATE_CMW" jobs --state "$REHEARSAL_ROOT/state" resume --json
"$CMW_MANAGED_PYTHON" -I -B - "$REHEARSAL_ROOT/state" "$SMOKE_JOB" "$SMOKE_ATTEMPT" <<'PY'
import sys, time
from cmw.jobs.lifecycle import read_job_with_identity_retry
from cmw.jobs.store import Store
store = Store(sys.argv[1])
job_id = int(sys.argv[2].removeprefix('J').split('.')[0])
deadline = time.monotonic() + 15
while time.monotonic() < deadline:
    job, _, _ = read_job_with_identity_retry(store, job_id)
    assert job['attempt_id'] == sys.argv[3], 'Attempt changed; stop'
    if job['status'] == 'Run' and job.get('group'):
        break
    assert job['status'] in {'Queue', 'Starting', 'Run'}, job['status']
    time.sleep(0.1)
else:
    raise TimeoutError('Attempt did not reach Run with a recorded group; no signal sent')
PY
"$CANDIDATE_CMW" jobs --state "$REHEARSAL_ROOT/state" ownership "$SMOKE_JOB" --role group --json
"$CANDIDATE_CMW" jobs --state "$REHEARSAL_ROOT/state" cancel "$SMOKE_JOB" --yes --json
"$CANDIDATE_CMW" jobs --state "$REHEARSAL_ROOT/state" show "$SMOKE_JOB" --json
```

**Expected:** a running group's identity is version 2 and VERIFIED. Current
`cancel --yes` requests the existing verified TERM-to-KILL cancellation; its
grace is 2 s. There is no implemented `--mode graceful|soft|hard`, and the draft
D14 10 s grace is not current behavior. A request is not a terminal result:
wait for the same attempt's terminal evidence and GONE observations. A cancelled
calculation is never a scientifically completed result.

**Rollback:** on UNVERIFIABLE or completion pending, retain the worker and its
evidence and wait for recovery; do not force Unknown or terminate the controller
to clear a hold. If bounded rehearsal waiting expires, stop the rehearsal and
report the pending state. Keep cleanup focused on its recorded sandbox identities.

### VASP inputs and stop semantics

The operator supplies `LICENSED_SI_INPUT`, an approved input directory outside
production calculations, and selects a new sandbox run directory. The approved
fixture is two silicon atoms, ENCUT 300, Algorithm Normal, a 2×2×2 k-point mesh,
EDIFF `1e-6`, NELM 40 and one MPI rank. Use the existing approved licensed input
directory in place, outside production calculations; do not copy its inputs
yourself or alter scientific parameters. Only the authorized runner stages its
required inputs into its private sandbox output. Never put
licensed content into Git, CI artifacts, an issue, the evidence archive or the
immutable runner snapshot.

For each separately approved case, choose a new `SMOKE_NAME`, an explicit
`SMOKE_TIMEOUT` in seconds and a `STOP_BEFORE` budget. Use `STOP_BEFORE=0` for
normal completion and the hard-cancellation case. For the scheduled stop case,
use the reviewed positive budget below the timeout. The following commands run
in Bash; the array preserves the exact checked argv.

```sh
export SMOKE_INPUT="$LICENSED_SI_INPUT"
export SMOKE_PARENT="$REHEARSAL_ROOT/runs/$SMOKE_NAME"
export SMOKE_OUTPUT="$SMOKE_PARENT/output"
mkdir -m 700 "$SMOKE_PARENT"
test ! -e "$SMOKE_OUTPUT"
"$CANDIDATE_CMW" vasp check-inputs "$SMOKE_INPUT" --json
runner_args=(--input "$SMOKE_INPUT" --output "$SMOKE_OUTPUT" --binary std
  --ranks 1 --ncore 1 --kpar 1 --mpi-mode synthetic --restart none
  --timeout "$SMOKE_TIMEOUT" --stop-before "$STOP_BEFORE" --managed-foreground)
"$CANDIDATE_CMW" vasp check-runner --runner "$RUNNER" --json -- "${runner_args[@]}"
"$CANDIDATE_CMW" jobs --state "$REHEARSAL_ROOT/state" add \
  --name "$SMOKE_NAME" --cwd "$SMOKE_INPUT" --engine VASP \
  --cpus 1 --mpi-ranks 1 --threads-per-rank 1 --memory-gib 1 --hold \
  --write-scope "$SMOKE_PARENT" --env CMW_JOBS_OWN_SESSION=1 \
  --env "CMW_MANAGED_PYTHON=$CMW_MANAGED_PYTHON" -- "$RUNNER" "${runner_args[@]}"
```

Use the returned job ID in the same review/release sequence as the benign job.
Record exact argv, input hashes, runtime and runner commits, immutable attempt,
receipt, drainage and completion evidence. The local trusted-argv route records
the pin explicitly; portable submission has no arbitrary environment field.
Here `synthetic` selects the port's supported MPI topology mode; the executable
is real `vasp_std`, not a fake engine. The write scope must already exist, while
the runner's output directory must not exist. In the rehearsal, using the
nonexistent output as the write scope refused before any run started.

The runner's `--stop-before` schedules a STOPCAR before its timeout. It is not
the future `cmw jobs cancel --mode soft` command. Nonzero budgets are accepted
by the managed invocation grammar. Successful producing-run association,
however, requires `advance_stop: not reached` for a nonzero budget, or
`advance_stop: disabled` for zero, and no stop/timeout. A reached scheduled stop
can establish the intended stop behavior and Jobs completion evidence; it cannot
be finalized as an accepted successful VASP result. For hard cancellation, wait
until the same attempt is `Run`, then issue the supported `cancel --yes`; an
already finished fixture does not exercise that case. Record natural completion,
scheduled stop and hard cancellation separately. Never weaken finalization
criteria to turn the latter two into successful results.

Before each release, check process names without entering any calculation:

```sh
"$CMW_MANAGED_PYTHON" -I -B - <<'PY'
import psutil
names = {'vasp_std', 'vasp_gam', 'vasp_ncl', 'mpirun', 'mpiexec',
         'prterun', 'orterun', 'orted'}
observed = [p.info for p in psutil.process_iter(['pid', 'name'])]
unavailable = [p['pid'] for p in observed if not p['name']]
assert not unavailable, ('Process names unavailable; cannot establish absence', unavailable)
active = [p for p in observed if p['name'] in names]
assert not active, active
print('No VASP/MPI process is running')
PY
```

The tiny fixture can finish before cancellation arrives. Two rehearsal attempts
completed naturally and their later cancel commands correctly refused. Warming
only the CLI import was insufficient while the driver enumerated descendants.
The successful driver warmed the public CLI before release and used the first
nonempty OUTCAR in the attempt's new, exclusive output as the VASP-start witness.
It then ran the same `cancel JOB --yes --json` parser and ownership guards. This
does not bypass control validation, send a direct signal or change scientific
inputs. For this short fixture, use this bounded cancellation driver with the
reviewed held job ID and output exported:

```sh
export SMOKE_JOB SMOKE_OUTPUT
"$CMW_MANAGED_PYTHON" -I -B - <<'PY'
import contextlib, io, os, time
from pathlib import Path
from cmw.cli import main
from cmw.jobs.lifecycle import read_job_with_identity_retry
from cmw.jobs.store import Store
state = str(Path(os.environ['REHEARSAL_ROOT']) / 'state')
job_id = int(os.environ['SMOKE_JOB'].removeprefix('J').split('.')[0])
output = Path(os.environ['SMOKE_OUTPUT'])
store = Store(state)
original, _, _ = read_job_with_identity_retry(store, job_id)
assert original['status'] == 'Hold'
try:
    output.lstat()
except FileNotFoundError:
    pass
else:
    raise RuntimeError('Output already exists; refuse to reuse its VASP-start witness')
assert '--output' in original['argv']
assert original['argv'][original['argv'].index('--output') + 1] == str(output)
def cli(*args):
    code = main(['jobs', '--state', state, *args, '--json'])
    assert code == 0, (args, code)
with contextlib.redirect_stdout(io.StringIO()):
    cli('status')  # Warm the complete public command path before release.
cli('resume')
cli('release', str(job_id))
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    job, _, _ = read_job_with_identity_retry(store, job_id)
    assert job['attempt_id'] == original['attempt_id']
    assert job['status'] not in {'Done', 'Fail', 'Cancelled', 'Unknown'}, job['status']
    try:
        started = (output / 'OUTCAR').stat().st_size > 0
    except FileNotFoundError:
        started = False
    if started:
        cli('cancel', str(job_id), '--yes')
        break
    time.sleep(0.05)
else:
    raise TimeoutError('No VASP output-start witness; cancellation not exercised')
PY
```

Still wait for terminal evidence and GONE afterwards. A returned cancel request
alone is not a pass. This is an operational qualification: no accepted scientific
artifact is published by the smoke procedure. Use `vasp inspect-result OUTPUT
--json`, the genuine attempt's `payload-exit.json`, the queue record, runner
metadata and XML completeness to report each outcome separately.

## 7. Production cutover and the other node: deferred gate

**Precondition:** all rehearsal results are reviewed; the Mini research job is
finished; original queues have no unresolved execution; the supported retirement
operation in step 1 is implemented, qualified and explicitly authorized for any
remaining confirmed-dead `Unknown`. D01 is unchanged. No command in this section
authorizes Mini access now.

1. Repeat the full 17-revision
   [deployed-runtime parity procedure](../architecture/deployed-runtime-parity.md#required-cutover-procedure)
   against the exact pinned `CMW_COMMIT` from §2, using actual installed
   evidence. Preserve source records and provenance tags. Resolve every
   unmatched protection and unverified runtime before deployment.
2. Qualify the exact candidate/runner installation on each machine in that
   machine's sandbox. The Mini's real-VASP runner smoke is a separate required
   result; MacBook results and synthetic CI do not substitute for it.
3. Apply the approved identity-preserving migration plan. Archive original
   configurations and provenance before switching references; do not move a
   retained enrolled queue/storage root. There is no supported generic migration
   command to insert here before that procedure is qualified. Retiring an
   attempt does not itself update aliases, runtime pins or registry references.
4. Use one production configuration, queue alias `main`, storage alias
   `scratch`, and `~/.config/cmw/nodes/connections.json` per machine. Freeze and
   enroll only the final qualified snapshots. Register each machine locally and
   the other via its explicit SSH alias. No Tailscale assumptions apply.
5. Generate each connection specification from the actual enrollment output as
   in step 4. For SSH, set `transport: "ssh"`, the existing `ssh_alias`, the
   verified `ssh_host_key` SHA-256 fingerprint, and the absolute `-I -B` endpoint
   wrapper path in `executable`. Its `config_root` is node-local. Do not copy
   machine-local paths from the other machine or update a host-key pin silently.

The supported observation commands after registration are:

```sh
"$PRODUCTION_CMW" nodes list --json
"$PRODUCTION_CMW" nodes check mbp_m3max --queue main --json
"$PRODUCTION_CMW" nodes check mm_m5pro --queue main --json
"$PRODUCTION_CMW" doctor --node mm_m5pro --queue main --json
"$PRODUCTION_CMW" jobs --node mm_m5pro --queue main status --json
"$PRODUCTION_CMW" jobs watch --all-nodes
```

**Expected:** both registries bind the approved UUIDs and runtime fingerprints;
all observations use the owner endpoint, not remote SQLite or a same-spelled
local path. SSH uses the existing direct-address route and a pinned host key.

**Rollback:** pause new dispatch, keep both immutable snapshots and original
evidence, and restore only the approved configuration/reference snapshot if no
new attempt was dispatched. Once a new attempt exists, do not roll back by
replacing its queue/runtime; preserve its owner and use a separately reviewed
recovery plan. A failed check never authorizes wiping and reenrolling production.

## 8. Deferred Mini guard-cost qualification

**Precondition:** an exclusive Mini qualification window after J4.1 has finished;
no production calculation running. This is not part of the MacBook rehearsal.

Follow [D34's measurement procedure](../architecture/multi-host-nodes.md#39-process-identity-and-supervision):
use observer base `0e22fdc` with its normal one-second budget and controller
cadence; record ten minutes idle and at least two hours with one synthetic busy
child per physical core. Repeat the candidate on the same machine and workload.
Use sandbox-installed environments and retain each child's v2 identity.

**Record:** OS, Python, cores, cadence, process count, each call's monotonic
elapsed time/outcome/reason, sample count, median/p95/p99/max, timeout and budget
exhaustion counts, recovery intervals, and calls made while a prior collector
is still running. The effective default coalescing interval is 5 s, justified by
the recorded 1–2 s recoveries in J4.1 guard events 109–208; it is configurable in
`[0, 60]` s and must not weaken admission.

**Rollback:** stop only the synthetic children whose recorded identities still
verify; establish GONE for every one. Retain samples and report any child or
observation that did not finish. Do not change production guard settings.

## 9. Cleanup and evidence retention

**Precondition:** all sandbox attempts are terminal, no completion is pending,
and the authorized process observation shows no conflicting calculation.
Preserve the recorded controller/worker/anchor identities
before stopping anything; a freshly captured unrelated PID is not sufficient.

```sh
"$CANDIDATE_CMW" jobs --state "$REHEARSAL_ROOT/state" pause --json
"$CANDIDATE_CMW" jobs --state "$REHEARSAL_ROOT/state" stop --json
"$CANDIDATE_CMW" jobs --state "$REHEARSAL_ROOT/state" status --json
```

Wait within a bounded cleanup deadline and verify GONE against every recorded
sandbox process identity, including any runner/MPI descendants. Do not treat
offline display, a missing PID file, `Path.exists() == false`, or an unreadable
probe as absence. If a fixture survives, report its exact identity and stop;
do not broaden signals to a name, process group or production process.

Remove every staged POTCAR **only from owned sandbox run directories**, including
runner-created input copies. Enumerate the selected run directories and reject
symlinks/unreadable entries; do not recurse through a symlink or a snapshot. Use
the exact verified file list for deletion, then verify each named file is absent.
No POTCAR bytes go into the retained report/archive, even if a run failed. Keep
only permitted identity/hash metadata. Do not upload full run artifacts.

Complete any authorized result-verification or successor-planning observations
**before** this cleanup. Removing required runner-staged POTCAR files
means later result verification can no longer re-establish the original source
closure. Report that post-cleanup limitation explicitly; a saved successful
check is historical evidence, not a currently verifiable result. This licensed
sandbox cleanup requirement does not authorize deleting the original licensed
input or production POTCAR. Both retain their original inputs and paths; D01
does not authorize replacing the retained queue/storage identities.

Record the final process census, cleanup results and sizes:

```sh
du -sk "$REHEARSAL_ROOT/snapshots" "$REHEARSAL_ROOT/state" \
  "$REHEARSAL_ROOT/storage" "$REHEARSAL_ROOT/runs" "$REHEARSAL_ROOT/evidence"
```

**Expected:** zero surviving owned rehearsal processes; no staged POTCAR remains;
retained private evidence contains commands, receipts/identities, checks and
counts, not licensed contents. Remove environments only after their owners are
GONE and their necessary evidence has been retained.

**Rollback:** an unreadable file/process is a failed cleanup check, never assumed
absent. Keep the private sandbox for diagnosis and report the remaining item;
do not remove its ownership evidence to make cleanup appear successful.

## 10. Deliberately deferred work

* [Issue #23](https://github.com/angzeli/computational-modelling-workflow/issues/23):
  durable completion proposal; do not infer crash recovery from in-memory retries.
* [Issue #24](https://github.com/angzeli/computational-modelling-workflow/issues/24):
  the separate first-domain handshake/proof work described by its proposal.
* [Issue #36](https://github.com/angzeli/computational-modelling-workflow/issues/36):
  automatic profile-pin enforcement and its complete operator upgrade path.
* [Issue #38](https://github.com/angzeli/computational-modelling-workflow/issues/38):
  result-source ctime sensitivity to authorized metadata-only changes. This is
  post-cutover work; it does not change D01 or authorize archive migration.
* [VASP port issue #4](https://github.com/angzeli/vasp-6.6.1-macos-arm64-port/issues/4):
  separately authorized output-buffering experiments. Qualification here does
  not enable broad unbuffering or alter production output policy.

Before acceptance, replace every unresolved checkpoint with the operator's
reviewed decision and evidence. Never turn an unresolved checkpoint into an
assumed success to make the procedure continuous.

## 11. MacBook rehearsal record — 9 November 2026

This record uses CMW `66a372a` and runner `3c438192`. It qualifies the sandbox
installation and the operations below; it does not deploy production or
qualify the Mini. J27.1 had stopped. No Mini contact or production queue,
registry, runtime, launcher or live-portal change occurred.

| Step | Result | Evidence and correction |
|---|---|---|
| Candidate pairing | Pass | `f3b0522..66a372a` changes tests/docs only. |
| Mini J4.1 retirement | Pending | Supported by PR #39; execute and verify §1 during the authorized window. D01 and all identities remain unchanged. |
| Saved deployed-revision parity | Pass, dated inventory only | All 17 revisions compared against `66a372a`: 16 ancestors; only `31616be` has an unmatched patch. Its receipt trap and denied-send protections are represented by PR #21 (`579f494`, `b33f298`), subsequent ownership-hold recovery and runner R. Installed-byte/live Mini parity remains a cutover-window check. |
| Snapshot build and order | Fixed, pass | Corrected wrapper to `-m cmw.cli`; exact Git objects select archives. Binary SHA-256 matches R. A post-baseline chmod preserves the content fingerprint but refuses security observation; final freeze precedes enrollment and runtime qualification. |
| Enrollment/readiness | Pass | Fresh private node, queue and storage. Full identity/runtime/storage/profile readiness true with no errors after execution; no writable snapshot members. |
| Doctor | Pass with informational limits | Observer startup isolated=1 and no-bytecode=1; effective no-bytecode=true; flags `-I -B`; owner cache access READ_ONLY. All dependencies/assets available. Installed-wheel source checkout is UNKNOWN informationally; the external snapshot commit record supplies source provenance. Completed workers are not live runtime attestations. |
| Status | Pass | Three fresh endpoint-backed calls after enrollment, plus later Doctor/portal/full-readiness checks; no self-triggered bytecode refusal. |
| VASP registration | Fixed, pass | An absent write-scope path refused before execution. Use an existing owner-only parent, nonexistent output, original licensed input, explicit managed interpreter and owned-session environment. Only the runner stages POTCAR. |
| Normal/STOPCAR/cancel | Pass | Results below. Two naturally completed cancellation trials remain recorded separately. The optional benign control fixture was unnecessary; real cases exercised these controls. |
| Portal | Pass | Real local backend in Textual's headless renderer at 120×40. Fresh, disconnected and recovered SVG captures. All five rows retained; first failure displays Last reported, disables controls and queues no mutation; recovery has a new handshake/status/session receipt. No backend reply was mocked. |
| Cleanup | Pass | All 23 recorded process identities GONE; zero VASP/MPI processes. All five staged POTCAR copies deleted; none remain. Original licensed input unchanged. |
| Mini scan-cost measurement | Deferred | Run §8 on the Mini only after J4.1 and the cutover prerequisites are resolved. |

| Case | Jobs outcome / receipt | Duration | Identity | OUTCAR / vasprun.xml | Scientific interpretation |
|---|---|---:|---|---|---|
| Normal, sandbox J1.1 | Done / exit 0 | 1.595 s | v2; worker/anchor GONE | Normal footer / complete XML | Native electronic convergence true; numeric check PASS. No accepted result artifact was published. |
| Runner STOPCAR, sandbox J2.1 | Done / exit 0 | 0.809 s | v2; worker/anchor GONE | Normal footer with requested-stop evidence / complete XML | LABORT requested at 0.1 s (`--timeout 60 --stop-before 59.9`); numeric convergence FAIL. Operational soft-stop pass, not scientific success. Runner removes its STOPCAR afterwards. |
| Cancel, sandbox J5.1 | Cancelled / exit 1 | 0.730 s | v2; worker/anchor GONE | No normal footer / incomplete XML | Public cancel accepted while Run; confirmed termination. No reuse or scientific-success claim. |

The two earlier cancellation trials (sandbox J3.1/J4.1) finished naturally in
0.793/0.944 s, with receipts of 0; later cancellation refused `Done`. These IDs
are scoped to the new sandbox queue and do not refer to Mini J4.1. Their
reproduction drove the bounded output-trigger correction above.

No cancellation was requested for the normal or STOPCAR cases. The successful
cancel completed before the current 2 s escalation threshold: source-path and
timing evidence indicate TERM without KILL escalation. The final job's signal
field is null; the current records do not persist a complete per-signal audit.
No kernel signal trace was collected, so this is not a forced-SIGKILL
qualification or an asserted exact signal-delivery log.

The read-only snapshots and retained sandbox evidence occupy approximately
211 MiB (209 MiB snapshots); disposable package caches were removed, freeing
approximately 191 MiB. The worktree's separate probe environment/evidence adds
approximately 4.7 MiB. Private receipts,
argv, identities, hashes, process checks and SVG captures remain outside Git.
No licensed bytes are included in this document or CI artifacts. Removing the
staged POTCARs intentionally prevents later full producing-run revalidation;
the retained checks describe the pre-cleanup rehearsal, not a currently complete
scientific source closure.


## Candidate refresh — 9 November 2026, CMW 8d4cfc3

The selected pair is CMW `8d4cfc3f71ee51696f4783c579b256f05d2e16a0` and
runner `4fd6fa7e4df1032a97703ce18fc6c033273d46ed` (port PR #6).
The complete runtime diff from rehearsed `66a372a` contains only
`src/cmw/jobs/retirement.py` and its CLI wiring in `src/cmw/jobs/cli.py`
(285 added lines); `scripts/` is unchanged. The runner diff from R changes only
`config/cmw-test-revision.txt`. Runner PR CI passed 43 tests and 2,226 fault
cases on each qualified platform.

A new MacBook sandbox used an installed wheel from the exact CMW archive,
then froze its snapshot **before enrollment**, and qualified it in fresh
`-I -B` processes. Full readiness, Doctor and three status calls passed.
Only a Command profile was needed for this retirement-only refresh; no VASP
binary or POTCAR was read or run. The earlier VASP rehearsal remains historical
and Mini real-VASP qualification remains pending.

The legacy fixture uses the provenance-checked Store and v1 identity function
from `deployed/31616be-j4-1`, writing an enrolled schema-4 Unknown record.
The rehearsal substituted its own PIDs and start dates for 90837/90838 and
2 November; its OUTCAR footer and complete XML were synthetic completeness
fixtures, not scientific evidence. The worker exited before the anchor check.
No controller was running, and the relevant ownership locks were free.

| Check | Demonstrated result |
|---|---|
| Live bash anchor, original v1 identity | VERIFIED; `ps`, SID and PGID matched the recorded fixture anchor. |
| Anchor removal | Identity revalidated immediately before positive-PID SIGKILL; reaped, then ownership GONE / exit 2. No group signal. |
| Retirement | RETIRED / exit 0; every reported identity GONE and both domain censuses empty; administrative Fail. |
| Compatibility | Queue identity and schema 4 unchanged; no manual database edits. |
| Admission | Unknown blocked the existing dispatch gate before retirement; the same gate accepted afterwards, then was paused again. No research job was dispatched. |
| Repeat | ALREADY_RETIRED / exit 0. |
| Live recorded member | REFUSED / exit 2; live member untouched. |
| Reused v1 PID | A deterministic recorded-birth mismatch at a live fixture PID returned UNVERIFIABLE / exit 75; occupant untouched. This simulates reuse without forcing kernel PID recycling. |
| Cleanup | All three rehearsal processes were reaped and verified GONE; the two processes from an initial driver argument-type error were also verified GONE. |

No runbook procedure discrepancy was found. The initial driver passed a numeric
job ID to `subprocess` without converting it to text; it was corrected before
repeating on a fresh fixture queue. Snapshot enrollment initially needed the
local execution permission that permits `diskutil` and low-priority scheduling;
no enrollment was completed by that refused attempt. These were harness and
execution-environment findings, not runtime fixes. No Mini contact or production
access occurred. Private command outputs and cleanup identities are retained
outside Git; the disposable refresh snapshot, queues and environment are removed
after evidence collection.
