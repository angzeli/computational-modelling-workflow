from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.periodic.vasp.result_sources import (
    SnapshotReader, SourceSnapshotError, read_json_source, revalidate_sources,
)


class ResultSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "OUTCAR"

    def observe(self, data=b"first\nsecond\n", **kwargs):
        self.path.write_bytes(data)
        reader = SnapshotReader(self.path, "OUTCAR", **kwargs)
        with reader:
            lines = list(reader)
        return lines, reader.record

    def test_exact_consumed_bytes_lines_and_offsets(self):
        data = "first\r\n\u03b1\nlast".encode()
        lines, record = self.observe(data)
        self.assertEqual([line.text for line in lines], ["first\r\n", "\u03b1\n", "last"])
        self.assertEqual([(line.line_number, line.byte_start, line.byte_end) for line in lines],
                         [(1, 0, 7), (2, 7, 10), (3, 10, 14)])
        self.assertEqual(record["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(record["bytes_read"], len(data))
        self.assertEqual(record["lines_read"], 3)
        self.assertEqual(record["stat_before"], record["stat_after"])
        self.assertEqual(record["stat_after"], record["path_stat_after"])
        self.assertEqual(record["coverage"], "complete")
        self.assertTrue(record["eof_reached"])
        self.assertTrue(record["stable"])
        self.assertEqual(self.path.read_bytes(), data)

    def test_empty_file_complete_snapshot(self):
        lines, record = self.observe(b"")
        self.assertEqual(lines, [])
        self.assertEqual(record["sha256"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(record["coverage"], "complete")

    def test_early_exit_cannot_claim_full_coverage(self):
        self.path.write_bytes(b"first\nsecond\n")
        with SnapshotReader(self.path, "OUTCAR") as reader:
            self.assertEqual(next(reader).text, "first\n")
        self.assertEqual(reader.record["coverage"], "incomplete")
        self.assertFalse(reader.record["eof_reached"])
        self.assertEqual(reader.record["sha256"], hashlib.sha256(b"first\n").hexdigest())
        self.assertFalse(revalidate_sources([reader.record])["valid"])

    def test_byte_limit_partial_line_not_yielded(self):
        self.path.write_bytes(b"first\nsecond\n")
        reader = SnapshotReader(self.path, "OUTCAR", max_bytes=9)
        with self.assertRaises(SourceSnapshotError) as caught:
            with reader:
                self.assertEqual(next(reader).text, "first\n")
                next(reader)
        self.assertEqual(caught.exception.code, "source.byte_limit")
        self.assertEqual(reader.record["sha256"], hashlib.sha256(b"first\nsec").hexdigest())
        self.assertEqual(reader.record["coverage"], "incomplete")
        self.assertEqual(reader.record["lines_read"], 1)

    def test_byte_limit_at_line_boundary_and_exact_file_size(self):
        self.path.write_bytes(b"first\nsecond\n")
        with self.assertRaises(SourceSnapshotError):
            with SnapshotReader(self.path, "OUTCAR", max_bytes=6) as reader:
                list(reader)
        self.assertLessEqual(reader.record["bytes_read"], 7)
        self.assertEqual(reader.record["coverage"], "incomplete")
        lines, record = self.observe(max_bytes=13)
        self.assertEqual(len(lines), 2)
        self.assertEqual(record["coverage"], "complete")

    def test_long_line_is_bounded(self):
        self.path.write_bytes(b"a" * 200)
        with patch("cmw.periodic.vasp.result_sources.MAX_LINE_BYTES", 32):
            with self.assertRaises(SourceSnapshotError) as caught:
                with SnapshotReader(self.path, "OUTCAR") as reader:
                    list(reader)
        self.assertEqual(caught.exception.code, "source.line_limit")
        self.assertEqual(reader.record["bytes_read"], 33)
        self.assertEqual(reader.record["coverage"], "incomplete")

    def test_invalid_utf8_has_partial_exact_digest(self):
        data = b"first\ninvalid \xff\nlast\n"
        self.path.write_bytes(data)
        with self.assertRaises(SourceSnapshotError) as caught:
            with SnapshotReader(self.path, "OUTCAR") as reader:
                list(reader)
        self.assertEqual(caught.exception.code, "source.encoding")
        self.assertEqual(reader.record["sha256"], hashlib.sha256(b"first\ninvalid \xff\n").hexdigest())
        self.assertEqual(reader.record["lines_read"], 1)
        self.assertEqual(reader.record["coverage"], "incomplete")

    def test_append_during_read_is_growth_and_incomplete(self):
        self.path.write_bytes(b"first\nsecond\n")
        with SnapshotReader(self.path, "OUTCAR") as reader:
            first = next(reader)
            with self.path.open("ab") as stream:
                stream.write(b"new\n")
            list(reader)
        self.assertEqual(first.text, "first\n")
        self.assertIn("growth", reader.record["changes"])
        self.assertFalse(reader.record["stable"])
        self.assertEqual(reader.record["coverage"], "incomplete")
        self.assertLessEqual(reader.record["bytes_read"], 14)

    def test_truncation_during_read_is_explicit(self):
        self.path.write_bytes(b"first\nsecond\n")
        with SnapshotReader(self.path, "OUTCAR") as reader:
            next(reader)
            self.path.write_bytes(b"first\n")
            list(reader)
        self.assertIn("truncation", reader.record["changes"])
        self.assertFalse(reader.record["stable"])
        self.assertFalse(revalidate_sources([reader.record])["valid"])

    def test_replacement_keeps_open_descriptor_bytes_and_detects_path_change(self):
        data = b"first\nsecond\n"
        self.path.write_bytes(data)
        with SnapshotReader(self.path, "OUTCAR") as reader:
            next(reader)
            replacement = self.root / "replacement"
            replacement.write_bytes(b"different\n")
            replacement.replace(self.path)
            list(reader)
        self.assertEqual(reader.record["sha256"], hashlib.sha256(data).hexdigest())
        self.assertIn("replacement", reader.record["changes"])
        self.assertFalse(reader.record["stable"])

    def test_removed_source_detected(self):
        self.path.write_bytes(b"first\n")
        with SnapshotReader(self.path, "OUTCAR") as reader:
            list(reader)
            self.path.unlink()
        self.assertIn("removed", reader.record["changes"])
        self.assertFalse(reader.record["stable"])

    def test_same_size_rewrite_and_restored_mtime_detected(self):
        _, record = self.observe()
        metadata = self.path.stat()
        self.path.write_bytes(b"other\nsecond\n")
        os.utime(self.path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        result = revalidate_sources([record])
        self.assertFalse(result["valid"])
        self.assertEqual(result["findings"][0]["code"], "source.identity_changed")

    def test_revalidation_detects_identical_bytes_in_replacement_inode(self):
        _, record = self.observe()
        replacement = self.root / "replacement"
        replacement.write_bytes(self.path.read_bytes())
        replacement.replace(self.path)
        self.assertFalse(revalidate_sources([record])["valid"])

    def test_revalidation_accepts_unchanged_source_without_writes(self):
        _, record = self.observe()
        before = self.path.stat()
        result = revalidate_sources([record])
        after = self.path.stat()
        self.assertTrue(result["valid"], result)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
        self.assertEqual(before.st_ctime_ns, after.st_ctime_ns)

    def test_missing_and_nonregular_sources(self):
        for path, code in ((self.path, "source.missing"), (self.root, "source.unsafe_path")):
            with self.subTest(path=path):
                with self.assertRaises(SourceSnapshotError) as caught:
                    with SnapshotReader(path, "OUTCAR"):
                        pass
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.record["coverage"], "incomplete")

    def test_symlink_file_and_parent_refused(self):
        self.path.write_bytes(b"first\n")
        alias = self.root / "alias"
        alias.symlink_to(self.path)
        parent_alias = self.root / "directory-alias"
        parent_alias.symlink_to(self.root, target_is_directory=True)
        for path in (alias, parent_alias / "OUTCAR", parent_alias / ".." / "OUTCAR"):
            with self.subTest(path=path):
                with self.assertRaises(SourceSnapshotError) as caught:
                    with SnapshotReader(path, "OUTCAR"):
                        pass
                self.assertEqual(caught.exception.code, "source.unsafe_path")

    def test_unreadable_source_has_structured_failure(self):
        self.path.write_bytes(b"first\n")
        with patch("cmw.periodic.vasp.result_sources.os.open", side_effect=PermissionError("denied")):
            with self.assertRaises(SourceSnapshotError) as caught:
                with SnapshotReader(self.path, "OUTCAR"):
                    pass
        self.assertEqual(caught.exception.code, "source.unreadable")

    def test_consumer_error_preserves_partial_record_and_propagates(self):
        self.path.write_bytes(b"first\nsecond\n")
        with self.assertRaisesRegex(ValueError, "consumer failure"):
            with SnapshotReader(self.path, "OUTCAR") as reader:
                next(reader)
                raise ValueError("consumer failure")
        self.assertEqual(reader.record["coverage"], "incomplete")

    def test_json_source_and_duplicate_nonfinite_or_malformed_rejection(self):
        self.path.write_text('{"name":"synthetic", "exit_code":0}\n')
        value, record = read_json_source(self.path, "receipt")
        self.assertEqual(value["exit_code"], 0)
        self.assertEqual(record["coverage"], "complete")
        for text in ('{"x": 1, "x": 2}', '{"x":NaN}', '{"x":Infinity}',
                     '{"x":1e999}', '{"nested":[{"x":-Infinity}]}', '{broken'):
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertRaises(SourceSnapshotError) as caught:
                    read_json_source(self.path, "receipt")
                self.assertEqual(caught.exception.code, "source.invalid_json")
                self.assertEqual(caught.exception.record["sha256"], hashlib.sha256(text.encode()).hexdigest())

    def test_invalid_limits(self):
        for value in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                SnapshotReader(self.path, "OUTCAR", max_bytes=value)


if __name__ == "__main__":
    unittest.main()
