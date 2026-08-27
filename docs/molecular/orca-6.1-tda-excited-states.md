# ORCA 6.1 TDA excited-state parsing and selection

## Supported grammar

`cmw.molecular.orca.excited_states` supports the fixture-tested
`orca_6_1_tda_v1` grammar:

- ORCA 6.1.x;
- an operative Tamm-Dancoff approximation;
- complete singlet-only or mixed singlet/triplet calculations;
- detailed `TD-DFT/TDA EXCITED STATES` sections;
- electric-dipole absorption spectra;
- optional velocity-dipole absorption spectra; and
- explicit excited-state completion plus normal ORCA termination.

Parsing fails closed with `UNSUPPORTED_EXCITED_STATE_FORMAT`,
`FAILED_EXCITED_STATE_PARSE`, or `FAILED_EXCITED_STATE_JOIN`. ORCA 5.x, ORCA
6.0, full TDDFT, SOC-corrected spectra, incomplete calculations, and outputs
without normal termination are not supported by this grammar version.

The quantitative parser is separate from `orca.status`. The status parser
continues to report advisory execution and section evidence; it does not
produce manuscript observables or decide scientific state identity.

## Canonical state identity

The stable cross-tool identity is:

```text
(spin_manifold, local_state_index)
```

`spin_manifold` is `singlet` or `triplet`, and `local_state_index` is one-based
within that manifold. The raw ORCA `STATE n` value is retained separately as
`orca_global_state_index`.

For example, a mixed calculation with 15 singlet roots prints the first
triplet as global `STATE 16`. Its canonical identity is `triplet/T1`, with
`local_state_index = 1` and `orca_global_state_index = 16`. A bare state
integer must not cross an ORCA/Multiwfn or artifact boundary.

Legacy `ExcitedStateRecord` mappings containing only `state_index` remain
readable. That field is interpreted as the canonical local index. New records
also serialize `canonical_identity` and the optional raw ORCA global index.

## Authoritative join

The detailed state blocks are authoritative for:

- raw ORCA global index;
- spin manifold, multiplicity, and printed `<S**2>`;
- manifold-local order;
- detailed energy in Hartree and rounded eV/cm⁻¹ values; and
- every printed orbital transition, contribution weight, and signed
  coefficient.

The electric-dipole spectrum is authoritative for higher-precision energy,
the printed wavelength, and the length-gauge oscillator strength. Wavelengths
are never recomputed when ORCA printed them. The velocity-dipole table is
joined independently and cannot overwrite the length-gauge value.

Rows are joined by `(spin_manifold, local_state_index)`, never physical row
order. The default detailed/spectrum energy tolerance is 0.002 eV, chosen to
cover the detailed block's three-decimal eV printing while still detecting a
wrong join. Duplicate identities, missing electric rows, mismatched
multiplicity, inconsistent gauge rows, or an energy difference beyond the
tolerance fail closed.

`primary_oscillator_strength_gauge` is `length` for the current grammar.
Triplet strengths printed as zero are preserved as numeric `0.0`. Signed
orbital coefficients are retained but their sign is explicitly phase
dependent and is not a stable chemical identifier.

The low-level record retains all transitions present in the supplied text. An
optional summary cutoff records the cutoff plus parsed, retained, and omitted
counts without deleting the underlying transition records.

## Protocol and provenance

The parser preserves ORCA version, functional, basis, TDA intent, requested
singlet/triplet roots, triplet generation, SMD solvent metadata, `TPrint` when
available, source path, source SHA256, source size, parser version, and tested
grammar version. An optional declared-protocol mapping is checked against the
output before records are returned.

When parser provenance and resolver results are supplied to
`create_excited_state_artifact`, CMW writes the additive `quantitative_v2`
contract. It records canonical selections, source-output identity, geometry
hash, scientific protocol hash, execution attempt, parser version, and grammar
version. Finalization does not require an NTO or Multiwfn artifact.

## Configurable non-CT selection

`StateSelectionRequest` and `resolve_excited_state` operate only on parsed
quantitative records. Supported policies are:

- lowest state in a configured manifold;
- lowest bright singlet using a configurable gauge and minimum strength;
- brightest singlet within a configurable energy above a reference state;
- selection from optional absolute energy and/or wavelength intervals, with a
  configurable brightest or lowest-energy preference.

The historical PDI threshold `f >= 0.01` is the default precedent, not an
immutable scientific constant. Every result records its normalized policy,
configuration, rationale, eligible candidate count, and deterministic
tie-break. Lowest-state ties use energy, local index, then raw ORCA index.
Brightness ties use oscillator strength descending, then energy and canonical
index ascending.

`lowest_ct_like_state` deliberately returns `DEFERRED`. This milestone does
not infer charge-transfer character from HOMO→LUMO dominance, oscillator
strength, NTO appearance, or a centroid distance. Fragment-resolved
hole/electron evidence is required before CT selection can be defined.

## Fixture provenance

The reduced ORCA 6.1.1 fixtures and their machine-readable provenance are in
`tests/fixtures/orca/excited_states/`. They retain the complete set of detailed
state headers, both oscillator-gauge tables, protocol evidence, completion,
and termination. Selected transition lines are copied verbatim from the large
`TPrint 1e-8` blocks. `provenance.json` records immutable source paths, source
hashes and sizes, exact extraction selections, reduced hashes and sizes, and
the statement that no scientific number was edited.
