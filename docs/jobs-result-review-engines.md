# Scientific result-review adapters

The Jobs review service calls existing scientific validators only when an explicit
inspection is requested. A recorded Jobs outcome is a separate operational domain.
There is no overall scientific PASS, and inspection never calls a publishing
finalizer, renames an output, updates a result record or modifies the queue.

## VASP

Review reuses `inspect_with_policy`, `inspect_binding` and `assess_result`; the
[existing VASP result-evidence contract](periodic/vasp-result-evidence.md) defines
native dialect, exact source identities, retrospective specifications, supported
policies, rounding and force constraints. No policy means no acceptance. Static
ionic relaxation is not applicable. A constrained force comparison retains its
explicit all/free scope; a fixed-atom maximum cannot substitute for a free-atom
policy. Unknown modes, incomplete coverage and missing binding remain qualified.
HSE, meta-GGA, response and unsupported ionic grammar are not expanded by review.

## ORCA and Multiwfn

Molecular review requires the immutable first-class Jobs molecular submission and
its exact prepared scientific attempt association. It rechecks current dependency
bytes and existing engine validation contracts. Bare output-file co-location and
live progress markers do not establish that association.

ORCA uses the existing native parser, `validate_stage` and `check_reuse` for SP,
OPT, FREQ and the supported OPT/FREQ/SP compositions. Each workflow stage keeps
its exact target, attempt and original layout. Electronic convergence, OPT/FREQ
outcome and current artifact integrity are separate. Default frequency inspection
reports facts without selecting a minimum/imaginary-frequency policy, even when
a prior runner had such a policy. The dialect remains the existing 6.1.x scope.

Multiwfn uses the existing FMO, ESP and IGMH validator through read-only
`inspect_analysis`, or the existing IFCT validator through read-only `inspect`.
The explicit finalizers delegate to these same checks and retain their original
publication behavior. IFCT inspection reads already normalized output paths;
only its finalizer may normalize them. The prepared IFCT ORCA alias must still
point to its exact original donor, which is recorded separately from the alias.
Cube completeness, geometry/grid checks, IFCT numerical/source/state rules and
runtime/menu identity checks are unchanged. No output is generated during review.

An optional molecular policy is a strict schema-1 JSON object. All five fields are
required; unknown keys, duplicate keys and non-finite numbers are rejected:

```json
{
  "schema_version": 1,
  "name": "explicit ORCA frequency review",
  "engine": "ORCA",
  "profile": "freq",
  "frequency": {
    "require_minimum": true,
    "imaginary_tolerance_cm1": 0
  }
}
```

`engine` and `profile` must exactly match the prepared submission. ORCA profiles
containing FREQ require both explicit frequency fields, using the unchanged
`FrequencyPolicy` semantics. All other profiles require `frequency: null`, including
Multiwfn `FMO`, `ESP`, `IGMH` and `IFCT`. Policy name, semantic hash and exact source
hash are retained. A policy PASS is restricted to these task-specific checks;
it does not qualify a real executable, physical-model accuracy or universal validity.
The shared service also requires the exact selected owner's successful native
operational evidence before retaining policy PASS.

## Bounds and reuse

The service supplies an explicit per-source byte limit and runs these synchronous
validators in a disposable observer process with its own wall-time deadline.
Cancel stops that observer only. Molecular sources are limited to the declared
closure (at most 512 files); no filesystem tree scan is performed. Full-file
validators are not called on over-limit sources. Native VASP readers preserve
incomplete coverage rather than accepting a prefix as complete history.

Source records preserve original paths, device/inode, size, modification/change
times and exact content hashes. The owner service distinguishes lightweight cached
observations from fresh full inspection and rechecks sources before export/reuse.
A source identity is a sequential snapshot, not an eternal certificate or an atomic
snapshot of multiple files. Existing finalizer records remain distinct from fresh
review evidence; review never refreshes their timestamps or acceptance state.
