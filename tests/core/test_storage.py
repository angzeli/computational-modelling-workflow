from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from cmw.core.storage import BYTES_PER_GIB, DiskUsage, StorageCapacityError, check_storage_capacity


class StorageCapacityTests(unittest.TestCase):
    def test_available_capacity_passes_with_structured_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = check_storage_capacity(
                Path(temporary),
                minimum_free_gb=100,
                usage_provider=lambda _: DiskUsage(
                    500 * BYTES_PER_GIB,
                    300 * BYTES_PER_GIB,
                    200 * BYTES_PER_GIB,
                ),
            )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["free_gib"], 200)
        self.assertEqual(result["minimum_free_gib"], 100)
        self.assertEqual(
            result["measurement"], "immediately_allocatable_filesystem_bytes"
        )

    def test_insufficient_capacity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                StorageCapacityError, "FAILED_STORAGE_CAPACITY"
            ):
                check_storage_capacity(
                    Path(temporary),
                    minimum_free_gb=100,
                    usage_provider=lambda _: DiskUsage(
                        500 * BYTES_PER_GIB,
                        450 * BYTES_PER_GIB,
                        50 * BYTES_PER_GIB,
                    ),
                )

    def test_missing_or_relative_check_path_fails_closed(self) -> None:
        with self.assertRaisesRegex(StorageCapacityError, "must be absolute"):
            check_storage_capacity(Path("relative"), minimum_free_gb=1)
        with self.assertRaisesRegex(StorageCapacityError, "is missing"):
            check_storage_capacity(Path("/definitely/missing/cmw-path"), minimum_free_gb=1)


if __name__ == "__main__":
    unittest.main()
