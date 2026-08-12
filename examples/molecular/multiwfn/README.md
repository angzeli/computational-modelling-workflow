# Multiwfn downstream examples

These examples document public invocation and layout. They intentionally do
not bundle wavefunctions or pretend that an unvalidated placeholder is a
scientific source. Substitute a CMW ORCA result manifest that records valid
`wavefunction` and `geometry` artifacts.

- [`fmo/README.md`](fmo/README.md) shows a non-executing HOMO/LUMO plan.
- [`esp/README.md`](esp/README.md) shows an ESP/density plan and thread override.
- [`igmh/README.md`](igmh/README.md) shows explicit fragment and grid files.

Deterministic executable integration is covered with the synthetic test double
under `tests/fixtures/`; routine tests never require Multiwfn.
