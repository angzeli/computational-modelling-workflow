"""Bounded identity checks consume only isolated synthetic input fixtures."""
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.progress.models import RunContext, SourceDescriptor
from cmw.progress.reader import MAX_PINNED_BYTES, RegularFileReader
from cmw.progress.vasp import VaspAdapter
from tests.periodic.vasp.result_fixtures import structure_text


class InputIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-pinned-input-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root/'POSCAR'
        self.raw = structure_text(flags=((True, True, True), (False, False, False))).encode()
        self.path.write_bytes(self.raw)
        self.source = SourceDescriptor('poscar', str(self.path),
            expected_sha256=hashlib.sha256(self.raw).hexdigest(), expected_size=len(self.raw))
        self.adapter = VaspAdapter(RunContext('pinned-test', 'VASP', (self.source,)))

    def reader(self, source=None, **kwargs):
        reader = RegularFileReader(source or self.source, **kwargs)
        self.addCleanup(reader.close)
        return reader

    def consume(self, reader, now=1):
        updates = []
        while True:
            update = reader.poll(now)
            self.adapter._read_poscar(update)
            updates.append(update)
            if not update.state.has_more:
                return updates
            self.assertIsNone(self.adapter.flags)
            self.assertEqual(update.state.health, 'VERIFYING')
            self.assertLess(len(updates), 1000)

    def test_backward_compatible_descriptor_and_output_reading(self):
        source = SourceDescriptor('stdout', str(self.path))
        self.assertIsNone(source.expected_sha256)
        self.assertIsNone(source.expected_size)
        self.assertEqual(self.reader(source).poll().state.health, 'OK')

    def test_multi_chunk_identity_waits_for_exact_complete_bytes(self):
        reader = self.reader(byte_budget=17)
        updates = self.consume(reader)
        self.assertGreater(len(updates), 1)
        self.assertTrue(all(update.state.bytes_read <= 17 for update in updates))
        self.assertEqual(updates[-1].state.total_bytes_read, len(self.raw))
        self.assertEqual(updates[-1].state.health, 'OK')
        self.assertEqual(self.adapter.flags, [[True]*3, [False]*3])
        self.assertEqual(reader.poll(2).state.bytes_read, 0)

    def test_hash_uses_raw_crlf_bytes_not_normalized_lines(self):
        raw = self.raw.replace(b'\n', b'\r\n')
        self.path.write_bytes(raw)
        source = replace(self.source, expected_sha256=hashlib.sha256(raw).hexdigest(), expected_size=len(raw))
        self.assertEqual(self.consume(self.reader(source, byte_budget=19))[-1].state.health, 'OK')
        self.assertEqual(self.adapter.flags, [[True]*3, [False]*3])

    def test_wrong_digest_clears_partial_text_and_error_is_cached(self):
        source = replace(self.source, expected_sha256='0'*64)
        reader = self.reader(source, byte_budget=23)
        update = self.consume(reader)[-1]
        self.assertEqual(update.state.health, 'INTEGRITY_ERROR')
        self.assertTrue(update.reset)
        self.assertIn('SHA-256', update.reason)
        self.assertIsNone(self.adapter.flags)
        self.assertEqual(self.adapter.poscar_text, '')
        again = reader.poll(2)
        self.assertEqual(again.state.health, 'INTEGRITY_ERROR')
        self.assertEqual(again.state.bytes_read, 0)
        self.assertFalse(again.lines)

    def test_changed_between_binding_and_first_read_rejected(self):
        self.path.write_bytes(self.raw.replace(b'T T T', b'F F F'))
        update = self.consume(self.reader())[-1]
        self.assertEqual(update.state.health, 'INTEGRITY_ERROR')
        self.assertIsNone(self.adapter.flags)

    def test_wrong_size_does_not_read_any_bytes(self):
        for size in (len(self.raw)-1, len(self.raw)+1):
            with self.subTest(size=size):
                update = self.reader(replace(self.source, expected_size=size)).poll()
                self.assertEqual(update.state.health, 'INTEGRITY_ERROR')
                self.assertEqual(update.state.bytes_read, 0)
                self.assertIn('byte size', update.reason)

    def test_malformed_identity_or_oversized_input_is_refused_without_read(self):
        for source in (
            replace(self.source, expected_sha256=None), replace(self.source, expected_size=None),
            replace(self.source, expected_sha256='bad'), replace(self.source, expected_size=True),
            replace(self.source, expected_size=-1), replace(self.source, expected_size=MAX_PINNED_BYTES+1)):
            with self.subTest(source=source):
                update = self.reader(source).poll()
                self.assertEqual(update.state.health, 'INTEGRITY_ERROR')
                self.assertEqual(update.state.bytes_read, 0)

    def test_same_inode_interior_rewrite_invalidates_previously_usable_flags(self):
        # Force the changed flags outside both ordinary 128-byte sentinels.
        raw = self.raw.replace(b'Selective dynamics', b' '*200+b'Selective dynamics') + b' '*200+b'\n'
        self.path.write_bytes(raw)
        source = replace(self.source, expected_sha256=hashlib.sha256(raw).hexdigest(), expected_size=len(raw))
        reader = self.reader(source, byte_budget=41)
        self.consume(reader)
        self.assertIsNotNone(self.adapter.flags)
        inode = self.path.stat().st_ino
        offset = raw.index(b'T T T')
        self.assertGreater(offset, 128)
        self.assertLess(offset+5, len(raw)-128)
        with self.path.open('r+b') as handle:
            handle.seek(offset)
            handle.write(b'F F F')
        self.assertEqual(self.path.stat().st_ino, inode)
        first = reader.poll(2)
        self.adapter._read_poscar(first)
        self.assertTrue(first.reset)
        self.assertIsNone(self.adapter.flags)
        update = self.consume(reader, 2)[-1]
        self.assertEqual(update.state.health, 'INTEGRITY_ERROR')
        self.assertIsNone(self.adapter.flags)

    def test_unchanged_bytes_with_new_signature_are_rehashed(self):
        reader = self.reader(byte_budget=31)
        first = self.consume(reader)[-1]
        before = self.path.stat()
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns+1_000_000))
        updates = self.consume(reader, 2)
        self.assertTrue(updates[0].reset)
        self.assertGreater(updates[-1].state.generation, first.state.generation)
        self.assertEqual(updates[-1].state.total_bytes_read, 2*len(self.raw))
        self.assertEqual(updates[-1].state.health, 'OK')

    def test_mutation_during_chunk_discards_that_chunk_and_constraints(self):
        reader = self.reader(byte_budget=31)
        self.consume(reader)
        self.assertIsNotNone(self.adapter.flags)
        before = self.path.stat()
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns+1_000_000))
        pread = os.pread
        def raced(fd, count, offset):
            result = pread(fd, count, offset)
            with self.path.open('r+b') as handle:
                handle.seek(self.raw.index(b'T T T'))
                handle.write(b'F F F')
            return result
        with patch('cmw.progress.reader.os.pread', side_effect=raced):
            update = reader.poll(2)
        self.adapter._read_poscar(update)
        self.assertEqual(update.state.health, 'INTEGRITY_ERROR')
        self.assertFalse(update.lines)
        self.assertTrue(update.reset)
        self.assertIsNone(self.adapter.flags)

    def test_symlink_input_and_redirect_after_validation_are_rejected(self):
        reader = self.reader()
        self.consume(reader)
        target = self.root/'other-POSCAR'
        self.path.rename(target)
        self.path.symlink_to(target)
        update = reader.poll(2)
        self.adapter._read_poscar(update)
        self.assertEqual(update.state.health, 'INTEGRITY_ERROR')
        self.assertIn('symbolic link', update.reason)
        self.assertIsNone(self.adapter.flags)
        self.assertEqual(self.reader().poll().state.health, 'INTEGRITY_ERROR')

    def test_replacement_with_same_valid_bytes_is_reverified(self):
        reader = self.reader()
        first = self.consume(reader)[-1]
        replacement = self.root/'new-POSCAR'
        replacement.write_bytes(self.raw)
        replacement.replace(self.path)
        second = self.consume(reader, 2)[-1]
        self.assertEqual(second.state.health, 'OK')
        self.assertTrue(second.reset)
        self.assertNotEqual(second.state.identity, first.state.identity)
        self.assertEqual(second.state.bytes_read, len(self.raw))

    def test_missing_newline_and_malformed_utf8_never_make_flags_usable(self):
        for raw in (self.raw.rstrip(b'\n'), b'\xff\n'+self.raw):
            with self.subTest(raw=raw[:10]):
                self.path.write_bytes(raw)
                source = replace(self.source, expected_sha256=hashlib.sha256(raw).hexdigest(), expected_size=len(raw))
                reader = self.reader(source, byte_budget=37)
                update = self.consume(reader)[-1]
                self.assertEqual(update.state.health, 'MALFORMED')
                self.assertIsNone(self.adapter.flags)
                self.assertEqual(reader.poll(2).state.health, 'MALFORMED')

    def test_permission_loss_and_disappearance_clear_previously_verified_flags(self):
        for failure, health in ((PermissionError('denied'), 'PERMISSION_DENIED'),
                                (FileNotFoundError('missing'), 'MISSING')):
            reader = self.reader()
            self.consume(reader)
            self.assertIsNotNone(self.adapter.flags)
            with patch('cmw.progress.reader.os.lstat', side_effect=failure):
                update = reader.poll(2)
            self.adapter._read_poscar(update)
            self.assertEqual(update.state.health, health)
            self.assertTrue(update.reset)
            self.assertIsNone(self.adapter.flags)
            restored = self.consume(reader, 3)[-1]
            self.assertEqual(restored.state.health, 'OK')
            self.assertEqual(restored.state.bytes_read, len(self.raw))
