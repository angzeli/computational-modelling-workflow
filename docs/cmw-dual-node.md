# Independent CMW execution nodes

CMW nodes expose remote access to separate local Jobs runtimes. Every node owns
its queue, controller, workers, admission decisions, scientific readers and
storage. The frontend receives observations and sends explicit requests. It does
not open remote SQLite, synchronize live state, pool resources or run cross-node
MPI. Closing an observer does not change execution state.

## Enrollment and observation

Ordinary `cmw jobs` commands retain local behavior and do not use the network.
Explicit `cmw nodes enroll --config NODE_CONFIG --spec NODE_SPEC.json` enrolls
local hardware, private queue states, directly attached storage and pinned runner
profiles. Enrollment refuses a live controller or active/unresolved attempt;
existing local states are not migrated by observation. Enrolled queues use Jobs
schema 4 so older runtimes fail closed instead of bypassing new preflight rules.
Retain old immutable runtimes for their existing unenrolled queues.

The node specification contains named `queues` (absolute private state paths),
`storages` (existing canonical directory roots), and `profiles` (engine, runner,
max_cpus, optional max_ranks and explicit dependencies). Node/queue/storage IDs are
independent UUIDs. Hardware identity, volume UUID and current mount evidence are
observed locally. A same-named replacement root is not the enrolled storage.
Neither enrollment nor readiness creates a missing calculation mount point.

`cmw nodes connect ALIAS --config FRONTEND_CONFIG --spec CONNECTION.json` checks
and enrolls an explicit connection. Its transport is `ssh` or local; SSH requires
an existing SSH alias, fixed absolute endpoint executable/configuration paths,
expected SSH SHA256 host-key fingerprint, persistent node/queue IDs, host ID and
actual runtime fingerprint. Hostnames and package version `0.1.0` are insufficient.
Aliases are display labels; all selected targets include node, queue, internal job
and immutable attempt identity.

```console
cmw nodes list --config FRONTEND_CONFIG --json
cmw nodes check mini --config FRONTEND_CONFIG --json
cmw jobs --nodes-config FRONTEND_CONFIG --node mini --queue research status --json
cmw jobs --nodes-config FRONTEND_CONFIG --node mini --queue research progress J1.1 --json
cmw jobs --nodes-config FRONTEND_CONFIG --node mini --queue research files J1.1 --json
```

## Protocol and observation boundaries

OpenSSH uses no PTY, batch authentication, strict host-key checking, no agent
forwarding and bounded timeouts. Only fixed, shell-quoted enrollment settings
cross the remote command-string boundary. Job names, paths and argv travel as
JSON data. The foreground `cmw nodes endpoint --config ... --queue ...` serves
protocol-1 JSON-lines with a 2 MiB frame bound, one in-flight request per channel,
strict request IDs/typed data and bounded diagnostic retention. Errors on stdout
are structured; no arbitrary shell/Python callback is exposed.

The endpoint retains bounded scientific observation sessions across requests.
Scientific source selection and validation, history reconstruction and log reads
stay on the owning node. The internal snapshot codec retains electronic notation
and existing bordered Scientific rendering metadata. Files include owning-node
and storage identities, relative path and node-local path; none is a permission
to open a same-spelled local path. Share opening is refused until endpoint/share
identity can be verified; Copy Location and explicit fetch are independent routes.

Snapshots separate endpoint observation sequence/session, node-local evidence
clock and transport receipt age. A reconnect does not adopt an old observer's
cursor or silently retarget a selection. Readiness distinguishes implementation,
configuration and qualification; no real-engine or physical dual-Mac acceptance
is implied by an isolated endpoint test.
