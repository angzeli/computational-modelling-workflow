from __future__ import annotations

import unittest

from cmw.core.execution_profiles import execution_profiles_from_mapping
from cmw.core.job import JobTarget
from cmw.molecular.orca.input import (
    OrcaStageSpec,
    resolve_orca_resources,
)
from cmw.molecular.orca.status import StageType


def _profile(*, nprocs: int = 8, memory_gb: int = 18):
    return execution_profiles_from_mapping(
        {
            "schema_version": 1,
            "active_profile": "test",
            "profiles": {
                "test": {
                    "orca": {
                        "nprocs": nprocs,
                        "total_memory_gb": memory_gb,
                    },
                    "multiwfn": {"nthreads": 8, "total_memory_gb": 18},
                }
            },
        }
    ).selected


class OrcaExecutionResourceTests(unittest.TestCase):
    def test_eighteen_gb_profile_derives_conservative_per_process_maxcore(self) -> None:
        resolved = resolve_orca_resources(_profile())

        self.assertEqual(resolved.resources.nprocs, 8)
        self.assertEqual(resolved.resources.maxcore_mb_per_process, 1843)
        self.assertEqual(resolved.derived_total_memory_mb, 14_744)
        self.assertLessEqual(resolved.derived_total_memory_mb, 18 * 1024)
        self.assertEqual(resolved.to_dict()["memory_safety_fraction"], 0.8)

    def test_resource_changes_do_not_change_scientific_target_identity(self) -> None:
        spec = OrcaStageSpec(
            StageType.SP,
            "DLPNO-CCSD(T) def2-TZVPP TightPNO",
            protocol={"method": "DLPNO-CCSD(T)", "basis": "def2-TZVPP"},
        )
        target = JobTarget("sp", "geometry", 0, 1, spec.scientific_identity())
        first = resolve_orca_resources(_profile(nprocs=8))
        second = resolve_orca_resources(_profile(nprocs=4))

        self.assertEqual(
            target.target_id,
            JobTarget("sp", "geometry", 0, 1, spec.scientific_identity()).target_id,
        )
        self.assertNotEqual(first.execution_profile_hash, second.execution_profile_hash)
        self.assertNotEqual(first.resources, second.resources)

    def test_invalid_safety_fraction_fails_closed(self) -> None:
        for fraction in (0, -0.1, 1.1):
            with self.subTest(fraction=fraction), self.assertRaises(ValueError):
                resolve_orca_resources(_profile(), safety_fraction=fraction)


if __name__ == "__main__":
    unittest.main()
