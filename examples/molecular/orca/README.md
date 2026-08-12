# ORCA molecular workflow examples

The four subdirectories call the same workflow entrypoint with one canonical
mode each. They share a small synthetic XYZ structure and JSON configuration.
The methods are illustrative configuration, not package defaults or a claim of
methodological hierarchy.

Inspect an example without launching ORCA:

```sh
./examples/molecular/orca/opt_freq_sp/run.sh --plan
```

Run it only after making `orca` available on `PATH` or setting `ORCA_EXE` to
your own licensed installation:

```sh
ORCA_EXE=/path/to/orca ./examples/molecular/orca/opt_freq_sp/run.sh
```

The examples never include or download ORCA itself.
