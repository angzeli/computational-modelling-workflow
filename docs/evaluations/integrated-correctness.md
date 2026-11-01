# Bounded integrated correctness campaign

This opt-in acceptance entry point composes the existing artifact gate, node
endpoint fixtures, synthetic VASP fixtures and metadata-history generator. It
is not part of every pull request and does not qualify scientific engines or a
physical remote node. Use an independent checkout and disposable external paths.
The private SSH phase requires macOS OpenSSH tools and permission to create a
loopback-only listener. It changes no system SSH configuration.

## Reproduction

Freeze and commit the complete candidate before building. Select an existing
build interpreter with Hatchling and uv with an already populated dependency
cache; the artifact gate installs offline. All output directories must be new.

```sh
PYTHON_BIN=/absolute/build-env/bin/python UV_BIN=/absolute/path/to/uv \
  bash scripts/ci/check_campaign.sh artifacts /absolute/campaign/artifacts
PYTHON_BIN=/absolute/campaign/artifacts/installed/env-editable/bin/python \
  bash scripts/ci/check_campaign.sh suite /absolute/campaign/offline-suite
CMW_CAMPAIGN_ARTIFACT_REPORT=/absolute/campaign/artifacts/installed-focus-jobs.json \
PYTHON_BIN=/absolute/campaign/artifacts/installed/env-jobs/bin/python \
  bash scripts/ci/check_campaign.sh soak /absolute/campaign/private-soak
```

Run the deterministic installed gates before the soak. The artifact phase builds
one wheel and sdist, validates distribution hygiene, installs core, Jobs, sdist
and editable modes, and executes the existing installed smoke in all four modes.
Additional focused invariant tests use copied test drivers outside the checkout
against the installed core and Jobs wheels. The full suite requires the verified
editable origin. The soak copies tests only, removes source-path environment
masking, and requires the successful installed Jobs report via
`CMW_CAMPAIGN_ARTIFACT_REPORT`. It verifies the exact qualified interpreter,
package origin and runtime fingerprint before starting fixture processes. Record its
runtime fingerprint and artifact digest with the frozen commit; an ancestor's
successful run does not certify changed source.

## Fixed resource and evidence bounds

The machine-readable predeclared budgets are `tests/campaign/support.py:BUDGETS`.
They cover 1,000/10,000 metadata rows, 50-row pages, two endpoint clients, zero
calculation payloads, one review worker, a 240-second wall deadline, bounded
source reads, encoded traffic, latency, per-process RSS and descriptors. The
whole-source review has its own explicit byte/deadline limits; it is not charged
as a live poll. Observe host load with the run and report failures without
increasing limits to conceal them.

The private soak exercises actual endpoint subprocesses over strict-key loopback
SSH, SFTP fetch and real PTY navigation. Synthetic native-shaped files supply
progress values. Local and federated screens are captured at 77x24, 80x24,
100x30 and 140x40. An unreachable second owner must not make cached data fresh or
prevent a new successful response from the healthy owner. Application JSON,
SSH channel counters, SFTP payload bytes and source-reader counters have distinct
scopes; none claims system-wide I/O accounting.

Cleanup is limited to recorded fixture process identities, with birth checks
before signalling descendants. Reports identify surviving or unobservable owned
identities and listener state. No host-wide process absence is asserted. Queue
and native-source bytes are checked for unintended changes; explicitly requested
private review caches and replica artifacts are separate outputs.

## Independent invariants and regressions

- `tests/progress/test_generated_schedules.py`: seeded legal delivery schedules,
  hand-specified energies and residuals, partial rows, native/source-local
  frontiers, fixed/movable force norms, and replacement/truncation invalidation.
- `tests/jobs/test_research_invariants.py`: exact-attempt hold/release models,
  independent catalogue joins, schema 2/3/4 review, durable registration retries,
  source changes, no-clobber publication and injected storage failures.
- `tests/nodes/test_integrated_portal.py`: queued observation callbacks complete
  with their original disconnect/runtime-mismatch refusal, including a result
  delivered before modal mount.
- `tests/jobs/test_integrated_navigation.py`: selection and viewport under
  search/modal/update/resize sequences, and refresh after base-widget unmount.

The campaign's external evidence records seeds, traces, minimal counterexamples
and physical disposable-source mutations. The representative faults are wrong
attempt acceptance, omitted successor source approval, omitted durable request
association, restored SP fallback, stale native context and missing viewport
restoration. Initial survivors are retained and explained, not hidden in an
aggregate mutation score. Mutated copies are never used for final artifacts.

## Qualification boundary

L1 unit/model evidence, L2 installed/subprocess evidence and L3 private loopback
SSH/SFTP/PTY evidence support the exact frozen candidate only. L4 physical-node
and L5 scientific-engine/production qualification remain unperformed. The
moving convergence-study feature is excluded until it has its own frozen
implementation and acceptance. This campaign neither changes execution consent
nor authorizes production activation, automatic research retries or publication.
