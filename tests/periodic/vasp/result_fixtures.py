"""Invented H/He native-output fixtures; no research or potential payloads.

The field layout follows the bounded native dialect. Values and geometries below
are human-chosen and independent of the parser, not output from a science engine.
"""
from pathlib import Path


CELL = ((4.0, 0.0, 0.0), (0.0, 5.0, 0.0), (0.0, 0.0, 6.0))
SKEW_CELL = ((4.0, 0.0, 0.0), (1.0, 5.0, 0.0), (0.5, 0.2, 6.0))
POSITIONS = ((0.5, 1.0, 1.5), (2.0, 2.5, 3.0))
FORCES = ((0.01, 0.002, 0.003), (0.3, 0.4, 0.0))


def structure_text(positions=POSITIONS, *, cell=CELL, flags=None):
    lines = ["Invented H He parser fixture", "1"]
    lines += [" ".join(f"{x:.10f}" for x in row) for row in cell]
    lines += ["H He", "1 1"]
    if flags is not None:
        lines.append("Selective dynamics")
    lines.append("Cartesian")
    for index, row in enumerate(positions):
        suffix = "" if flags is None else " " + " ".join("T" if x else "F" for x in flags[index])
        lines.append(" ".join(f"{x:.10f}" for x in row) + suffix)
    return "\n".join(lines) + "\n"


def native_texts(*, relaxation=False, evaluations=1, cell=CELL, positions=POSITIONS,
                 forces=FORCES, algorithm="DAV", native=True, footer=True,
                 nelm=3, ediff="0.10000000E-05", ediffg="-.20000000E-01",
                 terminal_de="-0.50000000E-07", terminal_deps="-0.40000000E-07",
                 version="6.6.1", settings=None):
    """Return independently understandable OUTCAR/OSZICAR/INCAR texts.

    Each evaluation has three electronic rows, with electronic energies near
    -2 eV but evaluated energies near -3 eV, so energy-role confusion is visible.
    Relaxations move only the first atom by 0.02 Angstrom per evaluation.
    """
    effective = {"EDIFF": ediff, "NELM": str(nelm), "NELMIN": "2", "NELMDL": "-5",
                 "EDIFFG": ediffg, "NSW": "10" if relaxation else "0",
                 "IBRION": "2" if relaxation else "-1", "ISIF": "2", "ISPIN": "1",
                 "ICHARG": "2", "ISTART": "0", "IALGO": "48" if algorithm == "RMM" else "38",
                 "ENCUT": "300", "ISMEAR": "0", "SIGMA": "0.05", "NELECT": "3",
                 "LHFCALC": "F", "LSORBIT": "F", "LNONCOLLINEAR": "F"}
    effective.update(settings or {})
    incar = "\n".join(f"{key} = {value}" for key, value in effective.items()) + "\n"
    out = [f" vasp.{version} invented synthetic fixture", " INCAR:", *incar.splitlines(),
           " POTCAR: invented identity, no potential data", " VRHFIN =H: synthetic",
           " VRHFIN =He: synthetic", " ions per type = 1 1", " NIONS = 2",
           *incar.splitlines(), " direct lattice vectors                    reciprocal lattice vectors"]
    # Only orthogonal/lower-triangular invented cells are used. Reciprocal rows
    # are printed for grammar completeness, and the parser uses direct columns.
    a, b, c = cell
    reciprocal = ((1 / a[0], -b[0] / (a[0] * b[1]),
                   (b[0] * c[1] - b[1] * c[0]) / (a[0] * b[1] * c[2])),
                  (0.0, 1 / b[1], -c[1] / (b[1] * c[2])), (0.0, 0.0, 1 / c[2]))
    out += [" ".join(f"{x:.7f}" for x in (*direct, *inverse))
            for direct, inverse in zip(cell, reciprocal)]
    out += [" position of ions in cartesian coordinates  (Angst):"]
    out += [" ".join(f"{x:.8f}" for x in row) for row in positions]
    osz = []
    endpoint = positions
    for index in range(evaluations):
        label = index + 1
        electronic = ("-2.000000000000", "-2.100000000000", "-2.100000050000")
        osz.append("       N       E                     dE             d eps       ncg     rms          rms(c)")
        for count, energy in enumerate(electronic, 1):
            out += [f"--------------------------------------- Iteration {label:6d}({count:4d})  ---------------------------------------",
                    " EDDAV: cpu time 0.0010: real time 0.0020" if algorithm == "DAV" else " RMM-DIIS: cpu time 0.0010",
                    f" free energy    TOTEN = {energy} eV"]
            de = terminal_de if count == 3 else ("-.20000000E+01" if count == 1 else "-.10000000E+00")
            deps = terminal_deps if count == 3 else "-.20000000E-01"
            rms_c = "" if count == 3 else " 0.20000000E-02"
            osz.append(f"{algorithm}: {count:3d} {energy} {de} {deps} 12 0.10000000E-03{rms_c}")
        if native is not None:
            out.append("------------------------ aborting loop because EDIFF is reached ----------------------------------------"
                       if native else "------------------------ aborting loop EDIFF was not reached (unconverged)  ----------------------------")
        endpoint = tuple(tuple(x + (0.02 * label if relaxation and atom == 0 and axis == 0 else 0)
                               for axis, x in enumerate(row)) for atom, row in enumerate(positions))
        out += [" POSITION                                       TOTAL-FORCE (eV/Angst)", " " + "-" * 83]
        out += [" ".join([*(f"{x:.5f}" for x in row), *(f"{x:.6f}" for x in force)])
                for row, force in zip(endpoint, forces)]
        out += [" " + "-" * 83, " total drift: 0.000000 0.000000 0.000000",
                " FREE ENERGIE OF THE ION-ELECTRON SYSTEM (eV)", " " + "-" * 51,
                " free  energy   TOTEN = -3.00000000 eV",
                " energy  without entropy= -2.99000000  energy(sigma->0) = -2.99500000"]
        osz.append(f"{label:4d} F= -.30000000E+01 E0= -.29950000E+01  d E =-.10000000E-03")
    if relaxation:
        out.append(" reached required accuracy - stopping structural energy minimisation")
    if footer:
        out += [" General timing and accounting informations for this job:",
                " Total CPU time used (sec): 2.0", " User time (sec): 1.8",
                " System time (sec): 0.2", " Elapsed time (sec): 2.2"]
    return {"OUTCAR": "\n".join(out) + "\n", "OSZICAR": "\n".join(osz) + "\n",
            "INCAR": incar, "endpoint": endpoint}


def write_run(directory, *, flags=None, **options):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    texts = native_texts(**options)
    cell = options.get("cell", CELL)
    (directory / "POSCAR").write_text(structure_text(options.get("positions", POSITIONS), cell=cell, flags=flags))
    (directory / "CONTCAR").write_text(structure_text(texts.pop("endpoint"), cell=cell, flags=flags))
    for name, text in texts.items():
        (directory / name).write_text(text)
    return directory
