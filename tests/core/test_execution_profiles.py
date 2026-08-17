from __future__ import annotations

from copy import deepcopy
import math
import tempfile
from pathlib import Path
import unittest

from cmw.core.execution_profiles import (
    execution_profiles_from_mapping,
    load_execution_profiles,
)


def _document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "active_profile": "local_mac",
        "profiles": {
            "local_mac": {
                "orca": {"nprocs": 8, "total_memory_gb": 18},
                "multiwfn": {"nthreads": 8, "total_memory_gb": 18},
            }
        },
    }


class ExecutionProfileTests(unittest.TestCase):
    def test_valid_profile_is_selected_and_has_stable_execution_identity(self) -> None:
        selected = execution_profiles_from_mapping(_document()).selected

        self.assertEqual(selected.name, "local_mac")
        self.assertEqual(selected.orca.nprocs, 8)
        self.assertEqual(selected.orca.total_memory_gb, 18.0)
        self.assertEqual(selected.multiwfn.nthreads, 8)
        self.assertEqual(
            selected.execution_profile_hash,
            execution_profiles_from_mapping(_document()).selected.execution_profile_hash,
        )
        self.assertIsNone(selected.orca.mpi)
        self.assertIsNone(selected.storage)
        self.assertNotIn("mpi", selected.orca.to_dict())

    def test_optional_storage_floor_is_typed_and_changes_execution_identity(self) -> None:
        configured = deepcopy(_document())
        configured["profiles"]["local_mac"]["storage"] = {
            "minimum_free_gb": 100
        }

        selected = execution_profiles_from_mapping(configured).selected

        self.assertEqual(selected.storage.minimum_free_gb, 100.0)
        self.assertEqual(
            selected.to_dict()["storage"], {"minimum_free_gb": 100.0}
        )
        self.assertNotEqual(
            selected.execution_profile_hash,
            execution_profiles_from_mapping(_document()).selected.execution_profile_hash,
        )

    def test_optional_mpi_runtime_paths_are_typed_and_change_execution_identity(self) -> None:
        configured = deepcopy(_document())
        configured["profiles"]["local_mac"]["orca"]["mpi"] = {
            "bin_directory": "/opt/runtime/openmpi/bin",
            "library_directories": ["/opt/runtime/openmpi/lib"],
        }

        selected = execution_profiles_from_mapping(configured).selected

        self.assertEqual(
            selected.orca.mpi.bin_directory, Path("/opt/runtime/openmpi/bin")
        )
        self.assertEqual(
            selected.orca.mpi.library_directories,
            (Path("/opt/runtime/openmpi/lib"),),
        )
        self.assertNotEqual(
            selected.execution_profile_hash,
            execution_profiles_from_mapping(_document()).selected.execution_profile_hash,
        )
        self.assertEqual(
            selected.to_dict()["orca"]["mpi"],
            {
                "bin_directory": "/opt/runtime/openmpi/bin",
                "library_directories": ["/opt/runtime/openmpi/lib"],
            },
        )

    def test_missing_or_unknown_active_profile_fails_closed(self) -> None:
        missing = _document()
        missing.pop("active_profile")
        with self.assertRaisesRegex(ValueError, "missing active_profile"):
            execution_profiles_from_mapping(missing)

        unknown = _document()
        unknown["active_profile"] = "local_mac_m3"
        with self.assertRaisesRegex(ValueError, "unknown active execution profile"):
            execution_profiles_from_mapping(unknown)

    def test_non_positive_and_non_finite_values_fail_closed(self) -> None:
        cases = (
            ("orca", "nprocs", 0),
            ("multiwfn", "nthreads", -1),
            ("orca", "total_memory_gb", 0),
            ("multiwfn", "total_memory_gb", math.inf),
        )
        for section, key, value in cases:
            with self.subTest(section=section, key=key, value=value):
                invalid = deepcopy(_document())
                invalid["profiles"]["local_mac"][section][key] = value
                with self.assertRaises(ValueError):
                    execution_profiles_from_mapping(invalid)

        invalid = deepcopy(_document())
        invalid["profiles"]["local_mac"]["storage"] = {"minimum_free_gb": 0}
        with self.assertRaises(ValueError):
            execution_profiles_from_mapping(invalid)

    def test_malformed_resource_sections_fail_closed(self) -> None:
        missing = deepcopy(_document())
        del missing["profiles"]["local_mac"]["orca"]["nprocs"]
        with self.assertRaisesRegex(ValueError, "malformed"):
            execution_profiles_from_mapping(missing)

        unsupported = deepcopy(_document())
        unsupported["profiles"]["local_mac"]["multiwfn"]["memory_keyword"] = 18
        with self.assertRaisesRegex(ValueError, "unsupported memory_keyword"):
            execution_profiles_from_mapping(unsupported)

        unexpected_top_level = deepcopy(_document())
        unexpected_top_level["scheduler"] = "local"
        with self.assertRaisesRegex(ValueError, "unsupported scheduler"):
            execution_profiles_from_mapping(unexpected_top_level)

    def test_invalid_mpi_runtime_paths_fail_closed(self) -> None:
        cases = (
            {
                "bin_directory": "relative/bin",
                "library_directories": ["/opt/runtime/lib"],
            },
            {
                "bin_directory": "/opt/runtime/bin",
                "library_directories": [],
            },
            {
                "bin_directory": "/opt/runtime/bin",
                "library_directories": "/opt/runtime/lib",
            },
        )
        for mpi in cases:
            with self.subTest(mpi=mpi):
                invalid = deepcopy(_document())
                invalid["profiles"]["local_mac"]["orca"]["mpi"] = mpi
                with self.assertRaises(ValueError):
                    execution_profiles_from_mapping(invalid)

    def test_yaml_loader_rejects_non_mapping_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "execution.yaml"
            path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be a mapping"):
                load_execution_profiles(path)


if __name__ == "__main__":
    unittest.main()
