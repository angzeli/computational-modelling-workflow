# Deployed schema-4 retirement fixture

`store.py.txt` is the byte-for-byte `src/cmw/jobs/store.py` at annotated tag
`deployed/31616be-j4-1`, peeled commit
`31616be474b2e2fd04ab6257c990983d46ddfcde`. `identity.py.txt` is the exact
`identity` function from that commit's `src/cmw/jobs/ownership.py`.
`provenance.json` records source paths, excerpt lines and SHA-256 identities.
These files let shallow CI checkouts exercise the deployed writer without a
network fetch or a second installation.

`tests/jobs/retirement_legacy_fixture.py` executes the unmodified historical
Store in a separate module. It enrolls a disposable schema-4 queue, adds a
synthetic Command job, and records Unknown plus v1 identities through the old
transaction/save APIs. The test captures the identities with the old identity
function while its synthetic processes are alive, then ends/reaps them before
invoking candidate retirement. No fixture contains a production identity,
queue, hostname, home path, calculation input or output.

The Store's imports resolve against the candidate installation. For this
Command fixture, the relevant add-time helpers only validate empty progress
declarations and return no VASP binding. They neither initialize nor write the
queue; the enrollment, schema, job record and all queue writes come from the
pinned historical Store. Layout, suspension, remote submission and scientific
input routes are not exercised by this fixture.

The candidate tests preserve the enrolled queue UUID and inode/path evidence.
The fixture does not enroll storage or exercise any storage mutation.
