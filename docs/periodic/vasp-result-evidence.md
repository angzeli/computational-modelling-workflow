# Bounded VASP result evidence

`cmw.periodic.vasp.results.inspect_result(directory)` reads existing output without
launching an engine, opening Jobs state or writing files. Evidence extraction,
caller-selected acceptance, and artifact finalization are separate operations.
No overall scientific acceptance is emitted without an explicit policy.

## Native dialect and modes

The supported native dialect is VASP **6.6.1** ordinary self-consistent static
calculations (`NSW=0`, `IBRION=-1`) and fixed-cell ionic relaxation (`ISIF=2`,
`IBRION=1/2/3`). Non-spin-polarized and collinear settings (`ISPIN=1/2`), IALGO
38/48 and DAV/RMM iteration tables are covered. The private read-only development
sample established the DAV path in 6.6.1; RMM and collinear variants have synthetic
coverage. Version banners alone never establish general 5.x/6.x support.

Variable-cell relaxation, MD, hybrid/meta-GGA, SOC/noncollinear, response/band,
magnetic constraints, external-force/field and machine-learned force-field modes
remain outside acceptance. Unsupported files can yield diagnostic facts. General
restart, WAVECAR/CHGCAR validity, model accuracy and energy comparability are
unassessed. This is external-runner integration, not a native VASP launcher.

## Evidence and correspondence

OUTCAR iteration boundaries establish chronological electronic evaluations;
OSZICAR (or explicitly selected stdout) supplies DAV/RMM rows. Correspondence
requires matching segment counts, native labels, every printed electronic
energy, final evaluated free energy and sigma-to-zero energy. It is not based on
co-location or blindly pairing lists. Multiple OUTCAR banners require an explicit
zero-based segment selection. An earlier footer cannot complete a later segment.
Earlier concatenated segments remain diagnostic for endpoint finalization.

Electronic-step energy and final `FREE ENERGIE OF THE ION-ELECTRON SYSTEM`
energy are distinct. Free energy, energy without entropy and sigma-to-zero energy
retain names, eV units and evaluation/source references. Native convergence,
reconstructed dE **and** d eps comparisons, iteration budget, and history coverage
remain separate. EDIFF=0 is fixed-count behavior. Reaching NELM can coincide with
convergence; fewer iterations do not establish it. Missing rms(c) is preserved.

Printed numbers retain their tokens and half-last-place bounds. A threshold
straddled by rounding stays unknown. No scientific tolerance is increased. See
[VASP EDIFF](https://vasp.at/wiki/EDIFF), the
[official clarification of both energy changes](https://alpha.vasp.at/forum/viewtopic.php?t=19143),
[OSZICAR](https://vasp.at/wiki/OSZICAR), and
[EDIFFG](https://vasp.at/wiki/EDIFFG). Positive EDIFFG is energy-based; negative is
force-based; zero is step-budget behavior. Energy-based relaxation acceptance is
not implemented. Native optimizer stopping is separate from force magnitude.
Trial evaluations and repeated labels are never called accepted optimizer steps.

A complete native timing footer, fatal/stop indicators, and an independently
recorded OS exit code are separate facts. No exit code is invented from a footer.
A present STOPCAR cannot establish when it acted; see the
[STOPCAR contract](https://vasp.at/wiki/STOPCAR).

## Periodic endpoint and constraints

The final complete geometry must belong to its force and evaluated-energy block.
POSCAR/CONTCAR reuse the existing parser, extended here only for exact matching
elemental row annotations and all-zero velocity tails emitted for non-MD runs.
Other suffixes, velocities and predictor/corrector sections remain unsupported.
The native OUTCAR species/counts determine the evaluated species identity.

Comparisons preserve cell basis and atom order. ASE's general minimum-image
calculation handles skew periodic cells; there is no sorting, rotation, symmetry
matching or standardization. Default representation tolerances are 2e-5 Angstrom
for positions and 2e-6 Angstrom for cell components, reflecting observed printed
precision. A policy may require a stricter comparison. Static endpoints are
compared with POSCAR; CONTCAR is not mandatory. Relaxations require the evaluated
endpoint to match CONTCAR, with the fixed cell and whole-atom constraints retained.

Force summaries retain raw vectors and all/free/fixed maxima, counts and zero-based
indices. Fixed-atom forces are not silently zeroed. An empty free set does not
prove relaxation. Partial-direction flags remain visible but their force
projection is unsupported: selective flags refer to direct-lattice directions,
including when coordinates are Cartesian. See
[POSCAR](https://vasp.at/wiki/index.php/POSCAR) and
[ISIF](https://vasp.at/wiki/ISIF).

Current POSCAR flags alone are not verified historical execution inputs. They are
reported as current-input-derived until an explicit binding is validated.
Matching native initial geometry and CONTCAR flags corroborates that binding;
it does not turn a retrospective declaration into a pre-run receipt.

## Source snapshots

OUTCAR and OSZICAR are streamed. Exact consumed bytes are hashed during parsing;
line/byte ranges accompany assertions. Device/inode, size and timestamp evidence
records growth, truncation, replacement and incomplete bounded reads. Numerical
summaries and endpoint geometry are retained, not eigenvalue/charge-grid text.
Non-finite source or derived values become explicit findings and null values,
never nonstandard JSON numbers. No whole-directory hashing is performed.

Snapshots are sequential observations, not an atomic filesystem snapshot. A quiet
file is not proof of completed execution. Finalization revalidates exact source
identities; arbitrary external edits can still race that final check. Reuse must
recheck the recorded sources, policy and bindings. Records describe snapshots,
not permanent certificates for paths.
