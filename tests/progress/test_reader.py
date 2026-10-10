from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.progress.models import SourceDescriptor
from cmw.progress.reader import RegularFileReader, READ_BUDGET


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw progress reader ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'native.out'

    def reader(self, **kwargs):
        reader = RegularFileReader(SourceDescriptor('native', str(self.path)), **kwargs)
        self.addCleanup(reader.close)
        return reader

    def test_file_version_metadata_tracks_appends_without_extra_reads(self):
        self.path.write_text('first\n')
        reader = self.reader()
        initial = reader.poll(1)
        info = self.path.stat()
        self.assertEqual(initial.state.file_version, (info.st_size, info.st_mtime_ns, info.st_ctime_ns))
        unchanged = reader.poll(2)
        self.assertEqual(unchanged.state.file_version, initial.state.file_version)
        self.assertEqual(unchanged.state.bytes_read, 0)
        with self.path.open('a') as handle:
            handle.write('next\n')
        appended = reader.poll(3)
        self.assertNotEqual(appended.state.file_version, initial.state.file_version)
        self.assertEqual(appended.state.generation, initial.state.generation)
        self.assertLessEqual(appended.state.bytes_read, len('next\n') + 256)

    def test_arbitrary_chunking_split_utf8_and_partial_lines(self):
        reader = self.reader(byte_budget=7)
        self.path.write_bytes(b'')
        text = 'first\n\u7e41\u9ad4\u4e2d\u6587 line\r\nlast\n'
        data = text.encode()
        records = []
        for index, byte in enumerate(data):
            with self.path.open('ab') as handle:
                handle.write(bytes((byte,)))
            update = reader.poll(index)
            records.extend(update.lines)
            self.assertLessEqual(update.state.bytes_read, 7+256)
        self.assertEqual([line.text for line in records], ['first', '\u7e41\u9ad4\u4e2d\u6587 line', 'last'])
        self.assertEqual([line.evidence.line_start for line in records], [1, 2, 3])
        self.assertEqual(records[1].evidence.byte_start, len(b'first\n'))
        self.assertEqual(records[-1].evidence.byte_end, len(data))
        self.assertEqual(reader.poll(100).state.pending_bytes, 0)

    def test_pending_line_does_not_emit_or_change_success_age_to_growth(self):
        self.path.write_bytes(b'pending')
        reader = self.reader()
        first = reader.poll(10)
        second = reader.poll(20)
        self.assertFalse(first.lines)
        self.assertEqual(first.state.pending_bytes, 7)
        self.assertEqual(second.state.last_success_at, 20)
        self.assertEqual(second.state.last_growth_at, 10)
        self.assertEqual(second.state.last_change_at, 10)
        self.assertEqual(second.state.bytes_read, 0)
        with self.path.open('ab') as handle:
            handle.write(b' finished\n')
        third = reader.poll(30)
        self.assertEqual(third.lines[0].text, 'pending finished')
        self.assertEqual(third.state.last_growth_at, 30)
        self.assertEqual(third.lines[0].observed_at, 30)

    def test_initial_catchup_and_append_have_bounded_reads(self):
        self.path.write_bytes(b'0\n'*(3*READ_BUDGET))
        reader = self.reader()
        update = reader.poll(1)
        self.assertEqual(update.state.bytes_read, READ_BUDGET)
        self.assertTrue(update.state.has_more)
        count = 1
        while update.state.has_more:
            update = reader.poll(count+1)
            self.assertLessEqual(update.state.bytes_read, READ_BUDGET+256)
            count += 1
        self.assertEqual(count, 6)
        before = update.state.total_bytes_read
        unchanged = reader.poll(10)
        self.assertEqual(unchanged.state.total_bytes_read, before)
        self.assertEqual(unchanged.state.bytes_read, 0)
        with self.path.open('ab') as handle:
            handle.write(b'new row\n')
        changed = reader.poll(11)
        self.assertLessEqual(changed.state.bytes_read, 256+len(b'new row\n'))
        self.assertEqual(changed.lines[0].text, 'new row')

    def test_truncation_replacement_and_same_inode_rewrite(self):
        self.path.write_text('original\n')
        reader = self.reader()
        one = reader.poll(1)
        self.path.write_text('x\n')
        two = reader.poll(2)
        self.assertEqual(two.reason, 'truncation')
        replacement = self.root/'new.out'
        replacement.write_text('rotated\n')
        replacement.replace(self.path)
        three = reader.poll(3)
        self.assertEqual(three.reason, 'replacement')
        with self.path.open('r+b') as handle:
            handle.write(b'edited!\n')
        four = reader.poll(4)
        self.assertEqual(four.reason, 'in-place rewrite')
        self.assertEqual([update.state.generation for update in (one,two,three,four)], [1,2,3,4])
        self.assertTrue(all(update.reset for update in (one,two,three,four)))
        self.assertEqual(four.lines[0].text, 'edited!')

    def test_tail_sentinel_detects_consumed_suffix_rewrite_before_append(self):
        self.path.write_bytes(b'first\n'+b'padding\n'*100+b'last\n')
        reader = self.reader()
        reader.poll(1)
        with self.path.open('r+b') as handle:
            handle.seek(-5, os.SEEK_END)
            handle.write(b'evil\nnew\n')
        update = reader.poll(2)
        self.assertEqual(update.reason, 'in-place rewrite')
        self.assertEqual(update.lines[-2].text, 'evil')

    def test_oversized_and_malformed_lines_are_bounded_and_skipped(self):
        reader = self.reader(byte_budget=8, max_line_bytes=12)
        self.path.write_bytes(b'x'*100+b'\n\xff\nvalid\n')
        lines = []
        while True:
            update = reader.poll(1)
            lines.extend(update.lines)
            self.assertLessEqual(len(reader._partial), 12)
            if not update.state.has_more:
                break
        self.assertEqual([line.text for line in lines], ['valid'])
        self.assertEqual(lines[0].evidence.line_start, 3)
        self.assertIn('Oversized source line omitted', update.state.limitations)
        self.assertIn('Malformed UTF-8 source line omitted', update.state.limitations)

    def test_file_disappearance_and_return_keep_read_position_when_identity_matches(self):
        self.path.write_text('one\n')
        reader = self.reader()
        first = reader.poll(1)
        away = self.root/'away'
        self.path.rename(away)
        missing = reader.poll(2)
        self.assertEqual(missing.state.health, 'MISSING')
        self.assertEqual(missing.state.last_success_at, 1)
        away.rename(self.path)
        self.assertEqual(reader.poll(3).state.generation, first.state.generation)
        self.assertFalse(reader.poll(4).lines)

    def test_fifo_and_directory_are_not_opened(self):
        os.mkfifo(self.path)
        reader = self.reader()
        with patch('cmw.progress.reader.os.open', side_effect=AssertionError('no FIFO open')):
            self.assertEqual(reader.poll().state.health, 'NOT_REGULAR')
        self.path.unlink()
        self.path.mkdir()
        self.assertEqual(reader.poll().state.health, 'NOT_REGULAR')

    def test_permissions_are_explicit_and_existing_age_is_preserved(self):
        self.path.write_text('one\n')
        reader = self.reader()
        reader.poll(1)
        with patch('cmw.progress.reader.os.stat', side_effect=PermissionError('denied')):
            update = reader.poll(2)
        self.assertEqual(update.state.health, 'PERMISSION_DENIED')
        self.assertEqual(update.state.last_success_at, 1)
        self.assertEqual(update.state.bytes_read, 0)

    def test_declared_directory_replacement_requires_new_binding(self):
        self.path.write_text('one\n')
        info = self.root.stat()
        reader = RegularFileReader(SourceDescriptor('native', str(self.path), root_path=str(self.root),
                                                    root_identity=(info.st_dev, info.st_ino+1)))
        self.addCleanup(reader.close)
        update = reader.poll(1)
        self.assertEqual(update.state.health, 'BINDING_CHANGED')
        self.assertEqual(update.state.bytes_read, 0)
        self.assertFalse(update.lines)

    def test_directory_replacement_during_read_discards_old_binding(self):
        directory = self.root/'declared'
        directory.mkdir()
        source = directory/'native.out'
        source.write_text('old evidence\n')
        info = directory.stat()
        reader = RegularFileReader(SourceDescriptor('native', str(source), root_path=str(directory),
                                                    root_identity=(info.st_dev, info.st_ino)))
        self.addCleanup(reader.close)
        pread = os.pread
        def racing(fd, size, offset):
            result = pread(fd, size, offset)
            directory.rename(self.root/'old-directory')
            directory.mkdir()
            (directory/'native.out').write_text('new evidence\n')
            return result
        with patch('cmw.progress.reader.os.pread', side_effect=racing):
            update = reader.poll(1)
        self.assertEqual(update.state.health, 'BINDING_CHANGED')
        self.assertFalse(update.lines)
        self.assertEqual(reader.poll(2).state.health, 'BINDING_CHANGED')

    def test_replacement_during_read_discards_old_evidence(self):
        self.path.write_text('old evidence\n')
        other = self.root/'replacement'
        other.write_text('new evidence\n')
        reader = self.reader()
        pread = os.pread
        def racing(fd, size, offset):
            result = pread(fd, size, offset)
            other.replace(self.path)
            return result
        with patch('cmw.progress.reader.os.pread', side_effect=racing):
            update = reader.poll(1)
        self.assertEqual(update.state.health, 'SOURCE_CHANGED')
        self.assertFalse(update.lines)
        self.assertEqual(reader.poll(2).lines[0].text, 'new evidence')

    def test_descriptor_reused_and_closed_explicitly(self):
        self.path.write_text('one\n')
        reader = self.reader()
        with patch('cmw.progress.reader.os.open', wraps=os.open) as opened:
            reader.poll(1)
            reader.poll(2)
            self.assertEqual(opened.call_count, 1)
        reader.close()
        self.assertIsNone(reader._fd)
        self.assertEqual(reader.poll(3).state.health, 'CLOSED')

    def test_relative_source_is_refused_and_unmounted_volume_is_explicit(self):
        reader = RegularFileReader(SourceDescriptor('native', 'relative.out'))
        self.addCleanup(reader.close)
        self.assertEqual(reader.poll().state.health, 'SOURCE_UNRESOLVED')
        volume = RegularFileReader(SourceDescriptor('native', '/Volumes/cmw-missing-test-volume/native.out'))
        self.addCleanup(volume.close)
        with patch('cmw.progress.reader.os.stat', side_effect=FileNotFoundError('unmounted')):
            self.assertEqual(volume.poll().state.health, 'VOLUME_UNAVAILABLE')


if __name__ == '__main__':
    unittest.main()
