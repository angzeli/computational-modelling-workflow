# Runtime observation policy 3: decision and safety argument

CMW checks that an explicitly enrolled local endpoint still uses the selected
implementation and filesystem authority before and after operations. It protects
against observable installation drift, replacement, unintended writable/ACL changes
and request execution under a different runtime. The enrollment file, selected
artifact, local account, Python/OS and verifier are trusted inputs. This is not
attestation against an actor controlling the verifier or privileged OS.

Implementation identity is the existing package-content digest plus interpreter,
version and dependency expectations. Policy 3 additionally retains member content
hashes for precise comparisons; these do not enter or alter the semantic fingerprint.
Explicit first enrollment establishes expectations from a complete read of the
operator-selected installation; acceptance independently compares all selected
installed members to the frozen wheel. Later checks compare to original enrollment,
never redefine expected bytes from a new scan.

Execution authority is canonical root/member/interpreter path and object identity,
member set, owners, modes, link counts, ACL/xattrs and platform security evidence.
Observation evidence is the raw timestamp/flag sequence, generation, completeness,
content-read count and bounded-work result. These are separate from authority.

The local Apple SDK sys/stat.h defines UF_TRACKED as 0x40 for document-ID tracking.
Policy 3 permits only that XOR, on Darwin regular package members, with equal
material stat/security fields and complete content verification. The bit alone is
not evidence of safety or an OS actor. Roots, directories, interpreter and symlinks
remain strict. Unexplained ctime-only changes refuse. Every observed unsafe boundary
is terminal even if a later read would show restoration.

One operation enumerates a fixed set (4096 files, 8192 entries, 16 MiB/member,
128 MiB/package), verifies each member through descriptor/path and complete native
security boundaries, and performs a final inventory/authority comparison. Each
member may undergo one qualified transition during that operation. A transition
after its content read requires one member-only reread; no package restart occurs.
At most two content reads/member, 256 MiB read bytes and 10 seconds of elapsed work
are allowed; the deadline is checked around bounded operations, not claimed to
preempt a blocked OS syscall. Partial results never become enrollment or authority.

Final comparisons use material identity and original content, retaining raw deltas.
This is a sequence of bounded observations, not an atomic multi-file snapshot.
It cannot detect every unobserved change-and-restore event or guarantee files never
change after their last observation. The supported account/OS must not maliciously
race or replace verifier/importer code. Those assumptions are explicit.

For policy-3 metadata revalidation, isolated source imports without existing cache
files and the original content/authority checks remain required. A positive
observation-only delta does not taint loaded code merely because ctime is newer
than process birth. Import-path/cache incoherence or a material/uncertain runtime
refusal taints that verifier; restoration cannot refresh it in place. A fresh
verified endpoint is required. No claim is made to hash Python interpreter memory.

Schema 3 is explicit. Schema 1/2 records retain exact-match checks only; unsupported
metadata revalidation requires separately authorized enrollment and is never silently
upgraded. Incomplete legacy security evidence is never filled retrospectively.
Before/after-dispatch diagnostics and same-request recovery remain unchanged.

Native security preserves the descriptor-bound Darwin filesec ACL contract:
successful fstatx_np and FILESEC_ACL query distinguish positive absence from errors;
present ACL serialization retains ACE order. Unknown/denied/partial native evidence
is a refusal. Strict API callers retain strict raw equality; only the regular-member
collector supplies the policy-3 boundary observer. The local SDK at
`$(xcrun --show-sdk-path)/usr/include/sys/stat.h`, lines 322–324 on the inspected SDK,
confirms the UF_TRACKED definition. Public primary reference:
[Apple XNU stat.h](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/stat.h).
No online retrieval or OS-actor attribution is claimed.

## Import policy on writable installations

The installed `cmw` launcher disables bytecode writes before importing the
package. Managed controllers, workers and payloads inherit a reserved
`PYTHONDONTWRITEBYTECODE=1` even if a job supplies another value. The receipt
helper also starts with `-B`. CMW's ORCA and Multiwfn runner scripts set the
no-write environment before starting their Python helpers.

Importing `cmw` as a library sets `sys.dont_write_bytecode = True` for
subsequent imports throughout that interpreter. Enrollment is recorded outside
the package and the same installation can serve multiple callers, so the policy
applies unconditionally. Python can still read existing caches. A third-party
interpreter that directly imports a cold package (including bare
`python -m cmw.cli`) can cache the package initializer before it executes.
This also applies to standalone source helpers that invoke bare `python -m`,
such as `xyz_to_poscar.sh` and generated HOF scripts run outside managed Jobs.
Use `-B` or set `PYTHONDONTWRITEBYTECODE=1` before launching them.

Independent processes, programs that reset the flag, explicit `compileall`
and package installers can still write to a writable installation. D31 therefore
requires a qualified read-only snapshot and a registered `-I -B` wrapper.

## Bytecode cache recovery

A status refusal with `bytecode_cache_changed` means the two boundaries differed
only in the named cache files and associated directory entries. The message
lists additions, removals and modifications. The node returns up to 2,048
characters of refusal text before the operation context. A long list of paths
can therefore be truncated in the remote message. The verifier keeps the full
path lists in its in-process diagnostic evidence. A modification can be an
object or metadata change. The verifier cannot identify the writer. Mixed changes,
incomplete security evidence and changes to non-bytecode files retain their
ordinary refusal reasons.

1. Record the original error, its operation and phase, cache paths, node, queue
   and any request ID. A refused status supplies no fresh jobs or telemetry.
   The observer stays tainted even if the previous files reappear.
2. On the owner, establish the cause against the qualified snapshot, including
   the exact interpreter, installed artifact, cache files and manifest where
   available. Existing bytecode can execute even under `-B`; the enrollment
   source hash alone cannot establish its provenance. If the change is
   unexplained, keep the endpoint out of service. Preserve evidence and running
   jobs. Leave a qualified replacement snapshot and any new pins to a separately
   authorized deployment.
3. Once the original installation and its authority are independently verified,
   stop the writer and check the registered executable's `-I -B` wrapper and the
   snapshot's read-only permissions. Close the tainted observer connection. For
   an embedded local backend, quit and reopen its portal or CLI. For an SSH
   endpoint, open a new verified endpoint session. Restarting the jobs
   controller leaves the observer's sticky refusal in place.
4. From the fresh observer, request capabilities, then read-only status for
   the same node and queue. A continuing refusal requires investigation of
   the reported evidence. A mutation whose response was lost must be looked
   up using the original request ID before taking any further action.

Doctor reports startup isolation and bytecode flags, the effective bytecode
setting, explicit launch switches and owner cache access. These are warnings for the operator. The verifier makes
its own complete observations before returning a response.

## Gate 1 focused independent review

Expectations below are handwritten; tests do not use the projection as their oracle.
The review follows raw objects through security reads, content reads and final
comparison, checking that no incomplete result is emitted and original trust remains
separate. There is no global metadata retry or quiet-window path. Member transitions
are bounded independently; a second observed flip of the same member in one operation
refuses. Across successful operations, original material authority/content remain
pinned while the last verified raw observation explains a later qualified delta.

| Transition | Required independent evidence / regression |
|---|---|
| Unchanged | Hand-computed SHA-256 of fixture bytes; exact fingerprint (`test_unchanged_runtime_and_complete_member_hashes`) |
| Late UF_TRACKED after startup | Same original content/security; isolation and coherent source paths, no ctime veto (`test_late_tracked_change_preserves_fingerprint_and_original_trust`) |
| A before open, B before final inventory | Two inventory calls, A read once/B twice, handwritten member hashes and byte total (`test_staggered_members_before_open_and_final_inventory_are_verified_without_restart`) |
| During initial/native ACL collection | Actual chflags/filesec calls; complete native evidence; no scan restart (`test_transition_during_initial_acl_collection_is_verified`, `test_flag_transition_inside_native_acl_property_query_is_recorded`) |
| During content read | Exactly one member reread, unchanged hash (`test_content_enclosing_transition_forces_one_member_reread`) |
| Content incl. same size/restored mtime + flag | Hash mismatch, including a late transition after old hashing (`test_content_with_restored_mtime_and_tracked_still_refuses`, `test_late_hidden_content_change_with_flag_cannot_use_old_hash`) |
| Inode/path/symlink | Real replacement/symlink refusal before adopting new authority (`test_same_bytes_replacement_and_symlink_refuse`) |
| Owner/mode/ACL/xattr/mixed flags | Handwritten stat/attribute boundaries and actual native ACL change; no reclassification (`test_original_authority_negatives_are_handwritten`, `test_observed_owner_and_xattr_changes_cannot_hide_behind_tracked`, `test_actual_acl_change_with_tracked_refuses_original_authority`) |
| Root/directory/interpreter/member set | Strict authority and actual membership mutation (`test_membership_and_unqualified_root_directory_interpreter_changes_refuse`, `test_actual_added_removed_members_during_final_scan_refuse`) |
| Incomplete baseline/security/content | Explicit refusal, no partial hashes published (baseline, partial-security and incomplete-read tests) |
| Observed material change then restore | First recorded unsafe boundary is terminal; verifier remains tainted (`test_material_change_then_restore_is_terminal_at_observed_boundary`, `test_material_refusal_taints_interpreter_after_disk_restoration`) |
| Repeated incoherence/deadline/bytes | One-transition/member and finite read/work limits, before publication (member-budget and deadline/byte tests) |
| Old/untrusted interpreter | Isolation/import-path/cache evidence; sticky refusal, original trust untouched (loaded-code tests) |
| Legacy schema 1/2 | Exact match only, no retrospective completeness or metadata-policy upgrade (`test_legacy_exact_only_and_no_retroactive_security`) |

This focused policy evidence does not qualify the whole application, real engines,
physical nodes or production deployment.
