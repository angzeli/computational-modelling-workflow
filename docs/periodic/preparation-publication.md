# Publishing prepared inputs and a Scratch record

VASP preparation uses two caller-selected destinations. A full calculation input
directory contains exactly `INCAR`, `KPOINTS`, `POSCAR`, and `POTCAR`. Its record is
`preparation.json` inside a separate, explicitly selected Scratch preparation
directory. Standalone molecular embedding publishes only its requested POSCAR;
its conversion metadata and atom mapping also live in the Scratch record.

Relative input destination and Scratch-root paths use the current working
directory. A relative record directory uses the Scratch root. Both destination
parents and the Scratch root must already exist. The resolver rejects existing
destinations, symlink traversal, overlapping input/record destinations, and
destinations that contain or overwrite a declared source. An optional expected
Scratch mount must be mounted and contain the root. On macOS, roots under
`/Volumes` also require their volume to be mounted; there is no fallback location.
Preview only resolves and checks these requirements and writes nothing.

## Publication sequence

The record and input directory may reside on different filesystems. CMW does not
claim an atomic transaction across them:

1. Revalidate destination identities and exclusively create the new Scratch
   preparation directory. No stale owner is reclaimed.
2. Write a versioned `intent` record containing exact candidate byte identities,
   destination locators, and the uniquely owned staging-directory locator.
3. Stage only the candidate input files in a private directory on the input
   destination's filesystem and flush those files and directory.
4. Publish without replacing an existing destination. Full directories use
   macOS `renamex_np(RENAME_EXCL)` or Linux `renameat2(RENAME_NOREPLACE)`; standalone
   files use a no-clobber hard link from their same-filesystem staging file.
5. Remove remaining owned staging data before finalization, then atomically write
   the Scratch record as `complete`. Success requires all these operations.

Each candidate is rendered and validated by its preparation adapter before this
sequence. The publication helper records the same in-memory bytes it writes.
It does not stage generated inputs in Git or launch calculations. A complete
preparation record is not scientific-result acceptance.

## Failure and inspection

An error before input publication records `failed` when possible and cleans only
the preparation's own staging directory. An error after input publication records
`incomplete` and never deletes the published inputs. Cleanup failure is reported
with its staging locator and diagnostic evidence; CMW does not report success or
perform broad cleanup. Abrupt interruption may leave `intent` plus uniquely named
staging data. Nothing automatically resumes, overwrites, or deletes these states.

Final record replacement and its directory sync are separate operations. If the
sync fails after a visible `complete` replacement, CMW attempts an explicit
`incomplete` replacement before syncing again. Even when that secondary sync
fails, a successful replacement leaves conservative visible evidence. If the
filesystem also refuses the corrective record replacement, the returned
`PUBLICATION_INCOMPLETE` error remains authoritative; an older visible record
cannot certify durability of the failed operation.

The read-only Python API checks state, expected names, and exact content identities:

```python
from cmw.core.preparation_publication import inspect_publication

result = inspect_publication("scratch/case/preparation.json")
assert result["exit_code"] == 0, result["findings"]
```

An optional `input_path=` inspects a supplied relocated destination while reporting
the changed locator. It does not change identities or repair publication. A
non-complete record, missing inputs, unexpected file names, or changed bytes
produces exit code 1 in this inspection result. The VASP checker can compose this
check with its supported input semantics when an explicit record is supplied.
Malformed JSON record shapes or a schema version other than integer `1` produce
structured invalid-record findings instead of escaping the inspection API.

## Durability limits

Files and directories use ordinary `fsync` and same-filesystem publication
primitives. They do not provide a multi-volume commit or a universal power-loss
guarantee; in particular, this implementation does not request macOS
`F_FULLFSYNC`. After an I/O error, abrupt termination, or storage disconnect,
inspect the record and inputs before taking any separate recovery action. The
contract protects cooperating preparers from merging or replacing one another's
destinations; it does not lock an entire filesystem against external modification.

Licensed potential bytes remain local inputs. Do not put real POTCARs, scientific
runtime outputs, or private source data into public Git history or package fixtures.
