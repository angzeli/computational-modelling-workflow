# FMO plan example

```sh
./scripts/workflows/generate_homo_lumo_cubes.sh \
  --source /path/to/validated-orca-result.json \
  --output /path/to/downstream-results \
  --grid-spacing-bohr 0.20 \
  --plan
```

The plan reports the source wavefunction identity, one-based HOMO/LUMO
identities, target ID, thread request, reuse decision, and expected
`homo.cube`/`lumo.cube` artifacts without creating an output directory.
