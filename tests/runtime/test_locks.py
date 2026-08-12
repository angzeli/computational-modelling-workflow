from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path

from cmw.core.locks import LockState, acquire_lock, inspect_lock, release_lock


class LockTests(unittest.TestCase):
    def test_active_owner_prevents_second_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "job.lock"
            owner = acquire_lock(path, job_id="test", owner_pid=os.getpid())
            self.assertEqual(inspect_lock(path).state, LockState.ACTIVE_LOCAL)
            with self.assertRaisesRegex(RuntimeError, "ACTIVE_LOCAL"):
                acquire_lock(path, job_id="other")
            self.assertFalse(release_lock(path, token="wrong"))
            self.assertTrue(release_lock(path, token=owner.token))

    def test_stale_local_lock_is_diagnosed_and_requires_explicit_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "job.lock"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "token": "old",
                        "hostname": socket.gethostname(),
                        "pid": 999_999_999,
                        "job_id": "old",
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(inspect_lock(path).state, LockState.STALE_LOCAL)
            with self.assertRaisesRegex(RuntimeError, "STALE_LOCAL"):
                acquire_lock(path, job_id="new")
            owner = acquire_lock(path, job_id="new", replace_stale=True)
            self.assertTrue(release_lock(path, token=owner.token))

    def test_remote_and_unreadable_ownership_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "job.lock"
            path.write_text("not-json", encoding="utf-8")
            self.assertEqual(inspect_lock(path).state, LockState.UNCERTAIN)
            with self.assertRaisesRegex(RuntimeError, "UNCERTAIN"):
                acquire_lock(path, job_id="new")
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "token": "remote",
                        "hostname": "remote-host",
                        "pid": 1,
                        "job_id": "remote",
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(inspect_lock(path).state, LockState.UNCERTAIN)


if __name__ == "__main__":
    unittest.main()
