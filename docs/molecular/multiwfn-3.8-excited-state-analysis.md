# Multiwfn 3.8 NTO and non-fragment hole/electron analysis

## Supported contract

CMW supports two fixture-tested Multiwfn 3.8 session grammars:

- `multiwfn_3_8_nto_v1` for natural transition orbitals; and
- `multiwfn_3_8_nonfragment_hea_v1` for non-fragment hole/electron analysis
  on the medium grid.

The renderer and parser versions are `1.0.0`. Other Multiwfn major/minor
versions fail closed. These contracts consume a selected quantitative
`ExcitedStateRecord` from the ORCA 6.1 TDA parser. They do not add support for
full TDDFT, SOC-corrected results, or production HOF excited-state execution.

## ORCA to Multiwfn state identity

The scientific identity remains `(spin_manifold, local_state_index)`. The raw
ORCA global index is provenance only. For example, the historical mixed ORCA
result maps global `STATE 17` to canonical `T2`, and the renderer sends local
triplet index `2` to Multiwfn.

Each rendered and parsed result preserves:

- canonical spin/local identity;
- ORCA global and local indices;
- Multiwfn local index;
- the ORCA source-output hash;
- the source `.molden.input` hash; and
- the selected-state energy cross-check.

Multiwfn prints state energies to three decimal places. The parser therefore
checks whether the higher-precision ORCA energy lies inside the interval that
would round to the printed Multiwfn value.

## Command and menu boundary

The existing `MultiwfnCommandSpec` remains the execution boundary. The
`.molden.input` wavefunction is the second process argument. The matching ORCA
`_tprint.out` is selected inside Multiwfn through stdin.

For singlet S1 NTO, the rendered sequence is:

```text
18
6
<absolute singlet ORCA _tprint.out>
1
3
S1_nto.mwfn
0
0
q
```

For triplet T2 NTO, it is:

```text
18
6
<absolute mixed ORCA _tprint.out>
3
2
3
T2_nto.mwfn
0
0
q
```

For singlet S1 HEA, the rendered sequence is:

```text
18
1
<absolute singlet ORCA _tprint.out>
1
1
2
10
1
11
1
0
0
q
```

For triplet T2 HEA, it is:

```text
18
1
<absolute mixed ORCA _tprint.out>
3
2
1
2
10
1
11
1
0
0
q
```

The menu text, menu SHA-256, execution layout, attempt identity, source
identities, source geometry hash, runtime/menu-contract requirement, and output
specifications are deterministic. A materialized stdin file must match the
rendered text exactly before the existing command adapter will construct the
process command. Finalization also requires the rendered geometry hash to match
the selected parent `ExcitedStateArtifact`.

## NTO parser

The NTO parser requires the version banner, loaded wavefunction, selected-state
line, matching state-summary row, ten-weight result block, cumulative weight,
and `.mwfn` export-completion marker. It returns ordered pair weights and
cumulative weights. An optional cumulative-weight cutoff records the minimum
pair count, retained indices, retained cumulative weight, and whether the
printed pairs reached the cutoff.

The historical session logs do not print the path typed at the `.mwfn` output
prompt and do not contain NTO hole/electron orbital labels. Those values are
therefore never invented: the output path comes from the renderer/output
specification, and orbital identities remain null unless separate evidence is
provided. Cube generation is a separate later Multiwfn operation; cube
references are optional provenance, not evidence produced by this NTO session.

## Non-fragment HEA parser

The HEA parser preserves `Sr`, optional `Sm`, printed `D`, signed `t`, hole and
electron centroids, componentwise printed `D`, spatial extents, `H`, `H_CT`,
hole/electron/transition-density integrals, optional HDI/EDI, grid origin/end/
spacing, grid dimensions and total points, and the printed cross-term
coefficient threshold. Units are explicit in the serialized result. Negative
`t` is a valid printed descriptor and is not rejected.

The historical session log does not echo the selected grid-menu integer. Grid
quality is therefore recorded as `medium` from the immutable renderer contract,
while the realized `134 x 90 x 44 = 530640` grid is parsed from the transcript.
This distinction is retained as `grid_quality_source = renderer_contract`.

## Precision-aware D validation

Multiwfn prints centroid coordinates to six decimal places and `D` to three.
Let `d_r` be the norm calculated from the printed centroids. Rounding each hole
and electron coordinate introduces at most `10^-p` uncertainty in each
component difference when the centroid precision is `p`; the Euclidean norm
therefore has an upper error bound of `sqrt(3) * 10^-p`. A printed `D` with
precision `q` represents a rounding interval of half-width `0.5 * 10^-q`.

CMW accepts the result only when the bounded interval around `d_r` overlaps the
printed-`D` rounding interval. It preserves both values, both precisions, both
intervals, the mathematical uncertainty, the consistency result, and policy ID
`decimal_rounding_interval_overlap_v1`. This accepts the historical rounded
`D = 0.000 Angstrom` values without replacing them or applying an arbitrary
large fixed tolerance, while still rejecting materially inconsistent values.

## Execution and artifact finalization

Parser completeness, process success, and artifact completeness are separate:

1. the parser validates the required result block and state mapping;
2. the execution layer must supply a captured exit code of zero; and
3. every required output must exist, be non-empty, occupy the exact rendered
   execution-layout path, and match the parsed session-log identity.

Only all three together can finalize `multiwfn_nto_v1` or
`multiwfn_nonfragment_hea_v1` artifacts. Historical transcripts record no
process exit code, so fixture parsing alone cannot produce a validated
production artifact. Schema-version-1 legacy artifacts remain readable; the
new contracts use additive metadata.

NTO and HEA are scientific siblings sharing the selected
`ExcitedStateArtifact`. HEA does not require NTO, so the stacking DAG no longer
uses NTO merely as an ordering dependency. Both nodes retain their own
execution layouts and canonical identity checks.

## Unsupported and deferred analysis

Fragment definitions passed to the non-fragment renderer raise
`DEFERRED_FRAGMENT_RESOLVED_HEA`. CMW does not infer fragment hole/electron
populations, donor/acceptor direction, fragment CT matrices, or
`lowest_ct_like_state`. A real, version-matched two-fragment HOF dimer
transcript is required before defining that grammar. The existing CT-like
state-selection request therefore remains deferred.

## Fixture provenance

Reduced verbatim fixtures and their byte identities are recorded in
`tests/fixtures/multiwfn/excited_states/provenance.json`. They originate from
the historical PDI S1/T1/T2 Multiwfn 3.8 session logs. Numerical text was not
edited; only unrelated menu repetition and progress animation were omitted by
documented line-range extraction.
