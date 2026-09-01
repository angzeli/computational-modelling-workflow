from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from cmw.cli import build_parser, main
from cmw.core.attempt_cleanup import CleanupExitCode, format_cleanup_plan
from tests.core.test_attempt_cleanup import SyntheticCampaign, _no_writer


class CleanupCliTests(unittest.TestCase):
    def test_json_dry_run_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "cleanup-attempts",
                        "--campaign",
                        str(fixture.root),
                        "--superseded-only",
                        "--json",
                    ]
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(code, CleanupExitCode.SUCCESS)
            self.assertEqual(payload["operation"], "DRY_RUN")
            self.assertFalse(payload["mutated"])
            self.assertEqual(len(payload["plan"]["plan_sha256"]), 64)

    def test_human_dry_run_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(
                    ["cleanup-attempts", "--campaign", str(fixture.root), "--superseded-only"]
                )
            self.assertEqual(code, CleanupExitCode.SUCCESS)
            self.assertIn("CMW cleanup plan", output.getvalue())
            self.assertIn("candidates:", output.getvalue())
            self.assertIn("blocked attempts:", output.getvalue())

    def test_missing_superseded_only_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            error = io.StringIO()
            with redirect_stderr(error):
                code = main(["cleanup-attempts", "--campaign", str(fixture.root)])
            self.assertEqual(code, CleanupExitCode.INVALID_ARGUMENT)
            self.assertIn("SUPERSEDED_ONLY_REQUIRED", error.getvalue())

    def test_exit_code_contract_for_blocked_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            fixture._write_campaign(canonical="attempt_002", watcher={"active": True})
            with redirect_stdout(io.StringIO()):
                code = main(
                    ["cleanup-attempts", "--campaign", str(fixture.root), "--superseded-only"]
                )
            self.assertEqual(code, CleanupExitCode.ACTIVE_CAMPAIGN)

    def test_help_contains_exact_examples_and_irreversible_warning(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()) as output:
                parser.parse_args(["cleanup-attempts", "--help"])
        text = output.getvalue()
        self.assertIn("cmw cleanup-attempts", text)
        self.assertIn("--confirm-plan FULL_SHA256", text)
        self.assertIn("irreversible", text)

    def test_format_is_concise_and_includes_reclaimed_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            text = format_cleanup_plan(fixture.plan())
            self.assertIn("estimated reclaim:", text)
            self.assertLess(len(text.splitlines()), 30)

    def test_json_error_contract(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "cleanup-attempts",
                    "--campaign",
                    "/definitely/missing/campaign",
                    "--superseded-only",
                    "--json",
                ]
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(code, CleanupExitCode.INVALID_PATH)
        self.assertEqual(payload["code"], "INVALID_PATH")


if __name__ == "__main__":
    unittest.main()
