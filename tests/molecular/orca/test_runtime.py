from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from cmw.core.execution_profiles import execution_profiles_from_mapping
from cmw.core.provenance import file_hash
from cmw.molecular.orca.runtime import (
    ORCA_REQUIRED_MPI_DATATYPES,
    OrcaRuntimeError,
    _inspect_macos_dependencies,
    _parse_ldd_dependencies,
    _parse_otool_dependencies,
    _parse_otool_rpaths,
    _parse_openmpi_fortran_datatypes,
    _probe_macos_loader,
    _resolve_macho_dependency,
    materialize_orca_runtime_contract,
    prepare_orca_runtime,
    runtime_environment,
    validate_orca_runtime_contract,
)


VALID_OMPI_INFO = "\n".join(
    f"compiler:fortran:have:{name}:yes" for name in ORCA_REQUIRED_MPI_DATATYPES
)


def _profile(root: Path):
    return execution_profiles_from_mapping(
        {
            "schema_version": 1,
            "active_profile": "test",
            "profiles": {
                "test": {
                    "orca": {
                        "nprocs": 4,
                        "total_memory_gb": 8,
                        "mpi": {
                            "bin_directory": str(root / "mpi" / "bin"),
                            "library_directories": [str(root / "mpi" / "lib")],
                        },
                    },
                    "multiwfn": {"nthreads": 2, "total_memory_gb": 4},
                }
            },
        }
    ).selected


class OrcaRuntimeTests(unittest.TestCase):
    def test_openmpi_fortran_datatype_capabilities_are_parsed(self) -> None:
        self.assertEqual(
            _parse_openmpi_fortran_datatypes(
                "compiler:fortran:have:integer4:yes\n"
                "compiler:fortran:have:complex16:no\n"
                "unrelated:value\n"
            ),
            {"integer4": True, "complex16": False},
        )

    def test_linker_output_parsers_preserve_dependency_identity(self) -> None:
        self.assertEqual(
            _parse_otool_dependencies(
                "binary:\n"
                "\tlibmpi.40.dylib (compatibility version 71.0.0, current version 71.1.0)\n"
                "\t@rpath/liborca.dylib (compatibility version 0.0.0, current version 0.0.0)\n"
            ),
            ("libmpi.40.dylib", "@rpath/liborca.dylib"),
        )
        self.assertEqual(
            _parse_otool_rpaths(
                "Load command 1\n"
                "          cmd LC_RPATH\n"
                "      cmdsize 32\n"
                "         path @loader_path/lib (offset 12)\n"
            ),
            ("@loader_path/lib",),
        )
        self.assertEqual(
            _parse_ldd_dependencies(
                "libmpi.so.40 => /opt/mpi/lib/libmpi.so.40 (0x01)\n"
                "libmissing.so => not found\n"
            ),
            (
                ("libmpi.so.40", "/opt/mpi/lib/libmpi.so.40"),
                ("libmissing.so", None),
            ),
        )

    def test_bare_mpi_library_resolves_only_from_configured_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owner = root / "orca" / "orca_startup_mpi"
            executable = root / "orca" / "orca"
            library = root / "mpi" / "lib" / "libmpi.40.dylib"
            for path in (owner, executable, library):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(path.name, encoding="utf-8")

            resolved = _resolve_macho_dependency(
                "libmpi.40.dylib",
                owner=owner,
                executable=executable,
                library_directories=(library.parent,),
                rpaths=(),
            )

            self.assertEqual(resolved, library.resolve())

    def test_unresolved_transitive_mpi_library_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "orca"
            helper = root / "orca_startup_mpi"
            for path in (executable, helper):
                path.write_text(path.name, encoding="utf-8")

            with patch(
                "cmw.molecular.orca.runtime._macho_dependencies",
                return_value=(("libmpi.40.dylib",), ()),
            ):
                with self.assertRaisesRegex(
                    OrcaRuntimeError,
                    "unresolved dynamic library libmpi.40.dylib",
                ):
                    _inspect_macos_dependencies(executable, (helper,), ())

    def test_darwin_overlay_materializes_verified_library_and_probes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            working = root / "attempt"
            working.mkdir()
            library = root / "mpi" / "libmpi.40.dylib"
            library.parent.mkdir()
            library.write_text("mpi", encoding="utf-8")
            record = {
                "runtime_id": "runtime",
                "platform": "Darwin",
                "launch_overlay": {
                    "strategy": "working_directory_symlink",
                    "files": [
                        {
                            "name": library.name,
                            "source": str(library.resolve()),
                            "size_bytes": library.stat().st_size,
                            "sha256": file_hash(library),
                        }
                    ],
                },
            }
            probe = {"status": "PASSED", "code": "VALID_ORCA_MPI_LOADER"}
            with (
                patch(
                    "cmw.molecular.orca.runtime.validate_orca_runtime_contract",
                    return_value=record,
                ),
                patch(
                    "cmw.molecular.orca.runtime._probe_macos_loader",
                    return_value=probe,
                ) as loader_probe,
            ):
                launch = materialize_orca_runtime_contract(
                    record,
                    orca_executable=root / "orca",
                    working_directory=working,
                )

            destination = working / library.name
            self.assertTrue(destination.is_symlink())
            self.assertEqual(destination.resolve(), library.resolve())
            self.assertEqual(launch["validation"]["status"], "PASSED")
            loader_probe.assert_called_once_with(
                record, working_directory=working.resolve()
            )

    def test_darwin_overlay_rejects_working_directory_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            working = root / "attempt"
            working.mkdir()
            library = root / "libmpi.40.dylib"
            library.write_text("expected", encoding="utf-8")
            (working / library.name).write_text("unrelated", encoding="utf-8")
            record = {
                "runtime_id": "runtime",
                "platform": "Darwin",
                "launch_overlay": {
                    "strategy": "working_directory_symlink",
                    "files": [
                        {
                            "name": library.name,
                            "source": str(library.resolve()),
                            "size_bytes": library.stat().st_size,
                            "sha256": file_hash(library),
                        }
                    ],
                },
            }
            with patch(
                "cmw.molecular.orca.runtime.validate_orca_runtime_contract",
                return_value=record,
            ):
                with self.assertRaisesRegex(
                    OrcaRuntimeError, "launch-overlay path already exists"
                ):
                    materialize_orca_runtime_contract(
                        record,
                        orca_executable=root / "orca",
                        working_directory=working,
                    )

    def test_loader_probe_detects_actual_dyld_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / "orca_startup_mpi"
            helper.write_text("helper", encoding="utf-8")
            record = {
                "dependency_validation": {"helpers_inspected": [str(helper)]},
                "environment": {
                    "path_prepend": [str(root)],
                    "library_path_variable": "DYLD_LIBRARY_PATH",
                    "library_path_prepend": [str(root)],
                },
            }
            result = subprocess.CompletedProcess(
                args=(),
                returncode=134,
                stdout="",
                stderr="dyld[1]: Library not loaded: libmpi.40.dylib",
            )
            with patch(
                "cmw.molecular.orca.runtime.subprocess.run", return_value=result
            ):
                with self.assertRaisesRegex(
                    OrcaRuntimeError, "loader probe found an unresolved"
                ):
                    _probe_macos_loader(record, working_directory=root)

    def test_parallel_runtime_rejects_openmpi_without_required_datatypes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orca = root / "orca" / "orca"
            startup = root / "orca" / "orca_startup_mpi"
            mpirun = root / "mpi" / "bin" / "mpirun"
            ompi_info = root / "mpi" / "bin" / "ompi_info"
            library = root / "mpi" / "lib" / "libmpi.40.dylib"
            for path in (orca, startup, mpirun, ompi_info, library):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(path.name, encoding="utf-8")
                path.chmod(0o755)
            with (
                patch("cmw.molecular.orca.runtime.platform.system", return_value="Darwin"),
                patch(
                    "cmw.molecular.orca.runtime._run",
                    side_effect=(
                        "mpirun (Open MPI) 4.1.6",
                        "compiler:fortran:have:real8:yes",
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    OrcaRuntimeError,
                    "lacks ORCA-required Fortran datatype support.*integer4",
                ):
                    prepare_orca_runtime(_profile(root), orca_executable=orca)

    def test_runtime_contract_is_revalidated_and_detects_binary_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orca = root / "orca" / "orca"
            startup = root / "orca" / "orca_startup_mpi"
            mpirun = root / "mpi" / "bin" / "mpirun"
            ompi_info = root / "mpi" / "bin" / "ompi_info"
            library = root / "mpi" / "lib" / "libmpi.40.dylib"
            for path in (orca, startup, mpirun, ompi_info, library):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(path.name, encoding="utf-8")
                path.chmod(0o755)
            dependency_result = {
                "format": "mach_o_otool",
                "dependency_edges": [
                    {
                        "owner": str(startup.resolve()),
                        "dependency": "libmpi.40.dylib",
                        "resolved": str(library.resolve()),
                    }
                ],
                "library_artifacts": {
                    str(library.resolve()): {
                        "size_bytes": library.stat().st_size,
                        "sha256": file_hash(library),
                    }
                },
            }

            with (
                patch("cmw.molecular.orca.runtime.platform.system", return_value="Darwin"),
                patch("cmw.molecular.orca.runtime.platform.machine", return_value="arm64"),
                patch(
                    "cmw.molecular.orca.runtime._run",
                    side_effect=lambda command, **_: (
                        VALID_OMPI_INFO
                        if Path(command[0]).name == "ompi_info"
                        else "mpirun (Open MPI) 4.1.6"
                    ),
                ),
                patch(
                    "cmw.molecular.orca.runtime._inspect_macos_dependencies",
                    return_value=dependency_result,
                ),
            ):
                record = prepare_orca_runtime(
                    _profile(root), orca_executable=orca
                )
                validated = validate_orca_runtime_contract(
                    record, orca_executable=orca
                )
                self.assertEqual(validated["runtime_id"], record["runtime_id"])
                self.assertEqual(
                    runtime_environment(record)["library_path_variable"],
                    "DYLD_LIBRARY_PATH",
                )

                mpirun.write_text("changed launcher", encoding="utf-8")
                with self.assertRaisesRegex(
                    OrcaRuntimeError, "identity no longer matches"
                ):
                    validate_orca_runtime_contract(record, orca_executable=orca)

    def test_parallel_runtime_requires_configured_mpi_paths(self) -> None:
        profile = execution_profiles_from_mapping(
            {
                "schema_version": 1,
                "active_profile": "test",
                "profiles": {
                    "test": {
                        "orca": {"nprocs": 4, "total_memory_gb": 8},
                        "multiwfn": {"nthreads": 2, "total_memory_gb": 4},
                    }
                },
            }
        ).selected
        with self.assertRaisesRegex(OrcaRuntimeError, "requires an MPI runtime"):
            prepare_orca_runtime(profile, orca_executable=Path("/missing/orca"))


if __name__ == "__main__":
    unittest.main()
