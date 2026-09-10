# Electronic array preparation

`cmw.analysis.electronic` is a NumPy-only, backend-neutral leaf module. It does
not import Matplotlib or pymatgen and performs no file I/O or process launch.
Install the checkout with `python -m pip install -e .` in the caller's environment.

- `shift_energy(values, reference_ev)` returns a detached, shifted array.
- `prepare_dos(energy, curves, reference_ev=..., divisor=...)` validates an
  increasing eV grid and matching physical density curves. One positive divisor
  applies to every curve. Its physical meaning and units belong to the caller.
- `aggregate_projections(elements, site_channels, groups)` sums explicitly
  labelled orbital components and sites without assumptions about species order.
  It does not force PAW projections to sum to total DOS.
- `prepare_path(lattice_rows, endpoints, samples, points_per_segment)` returns
  inverse-angstrom distances, intended unwrapped coordinates, segment slices and
  disconnected-branch indices. Reciprocal rows use `2*pi*inverse(A).T`.
  Ordered output coordinates may differ by integer reciprocal translations.
  Repeated endpoints stay repeated. Plot each segment slice separately; never
  draw a connecting line across disconnected endpoints.

Readers, composition rules, spin interpretation, reference selection, gap
classification, labels, styles and export remain caller responsibilities.
No spin channel is invented, mirrored or doubled by this API.

Run synthetic tests with `python -m unittest discover -s tests -p test_electronic_arrays.py`.
