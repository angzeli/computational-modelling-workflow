# Process health boundary

`cmw.core.process_health` follows one explicitly owned root PID and its
descendants. It aggregates sampled CPU use and resident memory, tolerates
descendant turnover, and can compare the size and modification time of
explicitly selected files. Small comparison state is written atomically to a
caller-selected path.

The default backend uses the POSIX `ps` command. The sampler and clock are
injectable so the classification remains deterministic in tests and another
platform backend can be added later.

Health is advisory operational evidence only. `ACTIVE`, `WAITING`,
`POSSIBLY_STALLED`, and `UNKNOWN` do not mean that a calculation terminated
normally, converged, or produced a scientifically valid result. The monitor
never terminates a process; that policy remains with its owning workflow.

## Startup workload release gate

`cmw.core.process_gate` is a separate read-only release predicate, not a health
classification or scientific validator. It snapshots caller-selected processes
for one UID, tracks PID/start-time identities and observed descendants across
reparenting, and ignores later independent matching workloads. Process groups
are followed only while their leader is an admitted live member, never merely
because a terminal or ancestor shares a group. Empty startup selection fails
closed unless the caller explicitly permits it; sampling errors also fail closed.

The standard-library `ps` backend has been smoke-tested on macOS. Linux runtime
compatibility remains unverified. Start times have one-second resolution;
same-second PID reuse is a residual limitation. Polling
cannot observe a child born and orphaned entirely between samples. A zombie is
treated as exited. The injected sampler, sleep and clock support deterministic
tests and caller-owned cancellation without signalling watched processes.
Executable-name selection, job paths, launch commands, settling periods and
campaign failure policies belong to callers, not this primitive.
