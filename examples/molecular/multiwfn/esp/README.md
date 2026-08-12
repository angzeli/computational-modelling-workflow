# ESP plan example

```sh
MULTIWFN_NTHREADS=4 \
./scripts/workflows/generate_esp_cubes.sh \
  --source /path/to/validated-orca-result.json \
  --output /path/to/downstream-results \
  --grid-spacing-bohr 0.20 \
  --plan
```

The plan reports the paired `density.cube`/`esp.cube` contract, source and grid
target identity, requested four-thread attempt, and reuse decision. It does not
expose internal Multiwfn menu numbers.
