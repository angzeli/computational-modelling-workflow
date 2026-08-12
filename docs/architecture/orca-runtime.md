# Provenance and ORCA execution lifecycle

The runtime distinguishes a **job target** from an **execution attempt**. A
target hashes scientific intent: stage type, geometry identity, charge,
multiplicity, and normalized ORCA calculation settings. An attempt records the
resources, executable evidence, exact generated input, time, and optional
parent attempt used to realize that target. Resource changes can therefore
create another attempt without pretending the scientific target changed.

Each attempt manifest is schema-versioned and atomically written. It records
the target, geometry lineage, execution and scientific states, factual ORCA
evidence, optional Git evidence, and SHA-256 artifact records. Git is optional;
a calculation directory need not be in a repository.

Reuse is fail-closed. Target identity, successful execution, valid scientific
status, every required artifact, its size, and its hash must agree. A file's
mere existence or an ORCA normal-termination marker is insufficient. A
non-reusable directory is not overwritten: callers must select a distinct
attempt directory.

Local stage locks contain host, PID, token, and job identity. Active local
ownership blocks mutation. Stale local ownership is diagnosed and can be
replaced only explicitly. Unreadable or remote ownership remains uncertain and
blocks mutation. These local locks do not solve scheduler or shared-filesystem
coordination.

`scripts/orca/run_orca.sh` owns executable discovery, locks, scratch space,
stdin isolation, file descriptors, process launch, signal forwarding, and
cleanup. It communicates with Python through JSON and stable exit codes. Python
owns target identity, ORCA evidence, validation, provenance, and reuse. Optional
macOS `caffeinate` support is enabled only with `CMW_CAFFEINATE=1`; it is never
required for correctness.
