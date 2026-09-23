# Ordered periodic structure model

`cmw.structure.periodic.PeriodicStructure` holds a represented periodic cell and
atoms in output order. It is an in-memory result, independent of publication,
execution, and the older molecular artifact contract. No charge or multiplicity
is inferred or required.

The cell is a tuple of three row vectors in ångström. Each `PeriodicAtom` carries
an element, fractional coordinates, Cartesian coordinates in ångström, a
zero-based `source_site_index`, a zero-based `expanded_index`, and a tuple of
symmetry-operation IDs. Cartesian coordinates must equal `fractional @ cell`
within an absolute tolerance of `1e-8` ångström, with zero relative tolerance.
The cell must be finite and have positive volume.

Both dataclasses are frozen; their sequence fields require tuples recursively.
They contain no mutable arrays or dictionaries. `to_dict()` produces detached
JSON-compatible lists and dictionaries, so changing a record representation
cannot change the model.

The atom tuple defines output order. Expanded indices must form a complete
bijection over `0..N-1`, even when species grouping changes that order. Several
expanded atoms can refer to one source site: the source-to-expanded relationship
is not a bijection. Full source rows and operation definitions belong in the
import record; the model retains the indices and IDs linking to that evidence.
The source-byte SHA-256 and selected CIF block identify its source snapshot.

`structure_id` hashes the represented cell, ordered elements, and fractional
coordinates rounded to **12 decimal places**. Cell precision is `1e-12` ångström;
fractional precision is dimensionless `1e-12`. This is decimal quantization,
not a distance-based equivalence test. Fractional coordinates are reduced modulo
one only for identity computation, including rounding across the unit boundary.
Integer lattice-image representations therefore share an identity; the stored
coordinates are never changed. Source bytes, labels, block name, provenance
indices and operation IDs do not affect this geometry identity.

Identity preserves the represented basis, origin and atom order. It does not
reduce primitive cells, apply symmetry equivalence, align origins, reorder atoms,
or certify scientific model equivalence. Cell construction, strain, matching,
slabs, stacking and interfaces are separate future operations.

`cmw.structure.ordering.group_species()` shares the existing molecular writer's
stable grouping rule: species follow first occurrence unless a complete explicit
order is supplied, and atoms within each species keep their input order. It
returns the species tuple and output-to-input permutation. The molecular
converter retains its existing cell, centering, mapping and publication behavior.
