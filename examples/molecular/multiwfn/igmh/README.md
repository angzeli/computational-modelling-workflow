# IGMH plan example

Create `fragments.json` using one-based indices and a complete, non-overlapping
partition:

```json
{
  "schema_version": 1,
  "indexing": "one_based",
  "allow_overlap": false,
  "require_complete_partition": true,
  "fragments": {"A": [1], "B": [2, 3]}
}
```

Create `igmh.json` with an explicit grid; this example value is illustrative,
not a package default:

```json
{
  "schema_version": 1,
  "profile": "interfragment",
  "grid_spacing_bohr": 0.20
}
```

Inspect the target without running Multiwfn:

```sh
./scripts/workflows/generate_igmh_cubes.sh \
  --source /path/to/validated-orca-result.json \
  --fragments fragments.json \
  --config igmh.json \
  --output /path/to/downstream-results \
  --plan
```

The plan reports the fragments, indexing convention, grid, source identity,
target ID, reuse decision, and expected `dg_inter.cub`/`sl2r.cub` pair. The
example does not encode any project-specific candidate-selection policy.
