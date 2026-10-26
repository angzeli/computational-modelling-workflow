# Selected-attempt scientific result review

`cmw jobs review JOB --json` returns metadata or session-cached evidence without
parsing scientific output. `cmw jobs review JOB --inspect --json` explicitly starts
one owner-node observer. The same route accepts existing `--state STATE` locally,
or `--node ALIAS --queue QUEUE` before `review` for an enrolled endpoint. Remote
paths are interpreted only on that endpoint; the frontend never opens them.

Examples (replace paths and identities with the selected real attempt):

```sh
cmw jobs --state /absolute/jobs-state review J7.1 --json
cmw jobs --state /absolute/jobs-state review J7.1 --inspect --json
cmw jobs --node mini --queue research review J7.1 --inspect --json
cmw jobs --node mini --queue research review J7.1 \
  --policy /owner/reviews/policy.json --spec /owner/reviews/binding.json \
  --max-bytes 67108864 --timeout-seconds 30 \
  --export /owner/reviews/new-review.json --json
```

Policy, specification, and export options explicitly request inspection. Policy
and export paths must be absolute owner-node paths. An existing VASP specification
retains its normal relative internal references. No policy means NOT ASSESSED;
there is no aggregate scientific PASS. A named policy can pass only within its
exact hashed scope and the selected producer's native completion boundary.

The seven independent domains are operational completion, source/input binding,
electronic convergence, ionic/analysis outcome, endpoint/output integrity, policy
assessment, and finalization/reverification. Static VASP ionic relaxation is N/A.
Done is a queue fact, not convergence. Unsupported modes and incomplete reads
remain qualified. The existing VASP whole-atom constraint, native/history,
precision, input/runner, policy and finalizer rules are reused unchanged.
[Engine policies and validator scope](jobs-result-review-engines.md) describe ORCA
and Multiwfn. Review does not run an engine, normalize outputs or call a finalizer.

## Bounded observer and UI

The selected-job **R Review** modal opens cached/metadata evidence first. **I
Inspect** explicitly starts background work, with optional policy/spec/export
fields. **C Cancel** stops only that observer; **Y Text** captures its frozen
report. Details and Copy use the same historical projection without parsing or
hashing output. The dashboard table, scientific card and Machine line are
unchanged. Returning from a modal preserves selection and viewport.

Each session permits one in-flight review, eight retained results and 128 request
identities. Inspection defaults to 64 MiB per source and a 30-second wall deadline;
explicit limits are 1 byte–1 GiB and 1–120 seconds. Molecular closures are limited
to 512 declared files. Native parsers retain partial coverage; incomplete reads
cannot establish complete-history acceptance. The observer subprocess is owned
by the frontend/endpoint session. Cancellation, deadline and session close stop
that process, never a calculation process. Reports are bounded for the wire.
No observer is started for every completed job on dashboard refresh.

Node operations `review.preview`, `review.start`, `review.poll`, `review.cancel`
require the exact enrolled node/queue/job/attempt. Start has an idempotent request
identity; changed options/target under the same ID fail. Handles expire with the
session and cannot be adopted after reconnect. Strict schema-1 typed review
capability is negotiated. Old workers are explicitly unsupported, with no local
fallback or interpretation of remote receipt paths. A disconnect retains cached
or pending observation, not a failed scientific execution. Polls release the
control channel between requests. Export additionally requires the existing
verified endpoint mutation guard and a writable connection.

## Source evidence, export and finalization

Review is retrospective, and exported snapshots/receipts are local evidence, not
tamper-proof attestations. Schema-2/3 remain supported; schema-4 exports additionally
retain and validate original queue enrollment and established submission lineage.
A schema-4 snapshot lacking enrollment must be re-exported from its original
queue. Downgrading it or copying a receipt/database does not grant authority.
See [execution evidence](periodic/vasp-result-evidence.md).

Exports are explicit fresh inspections, use exclusive creation, and must be
outside the selected calculation and Jobs-state directories. Existing files are
never replaced. Records include source paths/stat identities/hashes and coverage,
exact policy identity, runtime fingerprint/origin, owner identity and observation
time. Complete sources are rehashed before export; partial evidence remains
partial. Cache reopening performs only metadata freshness checks. Details/Copy
label cached results historical and remove current PASS claims. A changed source
invalidates reuse; rerun the explicit inspection and write a new record. There is
no import-as-authority or permanent certificate route. Sequential checks are not
an atomic snapshot and cannot prevent external writes after the check.

Finalization remains a separate explicitly authorized operation through the
existing VASP, ORCA or Multiwfn finalizers. Opening, inspecting, copying or exporting
a review never changes queue status, marks a job checked, publishes artifacts,
or modifies scientific inputs/outputs. VASP finalization/reverification stays
NOT ASSESSED when no final artifact record has been selected; use the existing
`cmw vasp verify-result-record RECORD` for that separate operation.

Qualification uses synthetic outputs from benign native Jobs workers and installed
local/private-loopback endpoints (L1/L2/L3). It does not establish real-engine
scientific acceptance or physical second-machine qualification (L4/L5).
