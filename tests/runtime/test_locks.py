from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from cmw.core import locks
from cmw.core.locks import LockState, acquire_lock, inspect_lock, release_lock


class LockTests(unittest.TestCase):
    def _interleave_guarded_mutations(self, first, second):
        import fcntl

        inspected = threading.Event()
        contending = threading.Event()
        original_inspect, original_flock = locks.inspect_lock, fcntl.flock
        results = {}

        def inspect(path, **kwargs):
            result = original_inspect(path, **kwargs)
            if threading.current_thread().name == "first":
                inspected.set()
                if not contending.wait(3):
                    raise AssertionError("Second actor never attempted the mutation guard")
            return result

        def flock(fd, operation):
            if threading.current_thread().name == "second" and operation == fcntl.LOCK_EX:
                contending.set()
            return original_flock(fd, operation)

        def run(name, operation):
            try:
                if name == "second" and not inspected.wait(3):
                    raise AssertionError("First actor never inspected ownership")
                results[name] = operation()
            except Exception as exc:
                results[name] = exc

        with patch.object(locks, "inspect_lock", side_effect=inspect), patch.object(fcntl, "flock", side_effect=flock):
            threads = [threading.Thread(target=run, args=(name, operation), name=name)
                       for name, operation in (("first", first), ("second", second))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive(), "Bounded lock contender did not finish")
        return results

    def test_competing_stale_replacers_preserve_exactly_one_successor(self) -> None:
        for iteration in range(20):
            with self.subTest(iteration=iteration), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "job.lock"
                stale = acquire_lock(path, job_id="stale", owner_pid=999_999_999)
                results = self._interleave_guarded_mutations(
                    lambda: acquire_lock(path, job_id="winner", replace_stale=True),
                    lambda: acquire_lock(path, job_id="loser", replace_stale=True),
                )
                winner = results["first"]
                self.assertIsInstance(winner, locks.LockOwner)
                self.assertIsInstance(results["second"], RuntimeError)
                self.assertIn("ACTIVE_LOCAL", str(results["second"]))
                self.assertEqual(inspect_lock(path).owner, winner)
                self.assertNotEqual(winner.token, stale.token)
                self.assertFalse(release_lock(path, token=stale.token))
                self.assertEqual(inspect_lock(path).owner, winner)
                self.assertTrue(release_lock(path, token=winner.token))

    def test_delayed_release_cannot_unlink_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "job.lock"
            stale = acquire_lock(path, job_id="stale", owner_pid=999_999_999)
            results = self._interleave_guarded_mutations(
                lambda: release_lock(path, token=stale.token),
                lambda: acquire_lock(path, job_id="successor", replace_stale=True),
            )
            self.assertIs(results["first"], True)
            self.assertIsInstance(results["second"], locks.LockOwner)
            self.assertEqual(inspect_lock(path).owner, results["second"])

    def test_normal_simultaneous_acquisition_grants_one_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "job.lock"
            ready = threading.Barrier(2)

            def contend(number):
                ready.wait(timeout=3)
                try:
                    return acquire_lock(path, job_id=str(number))
                except RuntimeError:
                    return None

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(contend, (1, 2)))
            winners = [result for result in results if result is not None]
            self.assertEqual(len(winners), 1)
            self.assertEqual(inspect_lock(path).owner, winners[0])

    def test_release_missing_parent_does_not_create_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "missing" / "job.lock"
            self.assertFalse(release_lock(path, token="missing"))
            self.assertFalse(path.parent.exists())

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
                acquire_lock(path, job_id="new", replace_stale=True)
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
            with self.assertRaisesRegex(RuntimeError, "UNCERTAIN"):
                acquire_lock(path, job_id="new", replace_stale=True)


if __name__ == "__main__":
    unittest.main()
