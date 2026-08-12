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
