"""Invented regressions for incomplete archived inputs and empty restart logs.

Real completed archives can omit their original POSCAR, while a failed restart
can leave an empty OSZICAR. Absence is not an observed cross-file disagreement.
No production coordinates, energies or output text are used here.
"""
from pathlib import Path
import tempfile
import unittest

from cmw.periodic.vasp.result_policy import assess_result
from cmw.periodic.vasp.results import inspect_result
from .result_fixtures import write_run
from .test_result_policy import policy


class MissingResultEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def test_missing_constraint_inputs_are_unknown_not_mismatched(self):
        for missing in (("POSCAR",), ("CONTCAR",), ("POSCAR", "CONTCAR")):
            with self.subTest(missing=missing):
                run = write_run(self.root / "-".join(missing), relaxation=True,
                                forces=((0.01, 0, 0), (0.01, 0, 0)))
                for name in missing:
                    (run / name).unlink()
                evidence = inspect_result(run)
                self.assertIsNone(evidence["constraints"]["contcar_flags_match"])
                assessed = assess_result(evidence, policy(relaxation=True, strict=False))
                self.assertEqual(assessed["status"], "UNKNOWN", assessed)
                checks = {c["code"]: c["status"] for c in assessed["checks"]}
                self.assertEqual(checks["constraints.endpoint_flags"], "UNKNOWN")

    def test_empty_electronic_table_is_unavailable_not_contradictory(self):
        run = write_run(self.root / "empty-table")
        (run / "OSZICAR").write_text("")
        evidence = inspect_result(run)
        self.assertEqual(evidence["electronic_table_correspondence"], "unavailable")
        self.assertFalse(evidence["conflicts"], evidence["conflicts"])
        assessed = assess_result(evidence, policy(strict=False))
        self.assertEqual(assessed["status"], "UNKNOWN")
        selected = policy(strict=False)
        selected["electronic"]["basis"] = "native"
        self.assertEqual(assess_result(evidence, selected)["status"], "PASS")

    def test_empty_restart_before_electronic_loop_stays_unknown(self):
        run = write_run(self.root / "restart", settings={"ISTART": "1", "ICHARG": "1"})
        text = (run / "OUTCAR").read_text()
        (run / "OUTCAR").write_text(text[:text.index("--------------------------------------- Iteration")])
        (run / "OSZICAR").write_text("")
        (run / "CONTCAR").write_text("")
        evidence = inspect_result(run)
        self.assertEqual(evidence["selected_segment"]["evaluations"], [])
        self.assertEqual(evidence["electronic_table_correspondence"], "unavailable")
        self.assertEqual(assess_result(evidence, policy(strict=False))["status"], "UNKNOWN")

    def test_observed_constraint_or_electronic_disagreement_still_fails(self):
        run = write_run(self.root / "disagreement", relaxation=True,
                        flags=((True, True, True), (False, False, False)))
        (run / "CONTCAR").write_text((run / "CONTCAR").read_text().replace("F F F", "T T T"))
        evidence = inspect_result(run)
        self.assertIs(evidence["constraints"]["contcar_flags_match"], False)
        self.assertEqual(assess_result(evidence, policy(relaxation=True, strict=False))["status"], "FAIL")
        (run / "OSZICAR").write_text((run / "OSZICAR").read_text().replace("-2.000000000000", "-9.000000000000"))
        evidence = inspect_result(run)
        self.assertEqual(evidence["electronic_table_correspondence"], "conflict")


if __name__ == "__main__":
    unittest.main()
