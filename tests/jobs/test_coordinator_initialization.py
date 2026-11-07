"""Context publication recovery never adopts or overwrites another channel."""
from contextlib import ExitStack
import errno
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cmw.jobs import suspension


class CoordinatorInitializationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-context-publication-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.store = SimpleNamespace(root=root, path=root/'queue.sqlite3')
        self.store.path.write_text('synthetic state identity')
        (root/'attempts'/'attempt').mkdir(parents=True)
        self.directory = root/'attempts'/'attempt'/'suspension'
        self.job = {'id': 1, 'attempt_id': 'attempt', 'claim': 'claim',
                    'worker': {'pid': 11}, 'suspension': {'backend': 'local-command-v1'}}
        self.anchor = {'pid': 12}
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(patch('cmw.jobs.suspension_protocol.clock_descriptor',
                                      return_value={'kind': 'fixture', 'boot_session_id': 'fixture'}))
        self.stack.enter_context(patch('cmw.jobs.suspension_protocol.now_clock', return_value=1.0))
        self.sleep = self.stack.enter_context(patch.object(suspension.time, 'sleep'))

    def check_recovery(self, point):
        faulted, creations, publications = [], [], []
        mkdir, dump, fsync, link, unlink = Path.mkdir, json.dump, os.fsync, os.link, os.unlink

        def fail():
            if not faulted:
                faulted.append(point)
                raise OSError(errno.EIO, 'one fixture publication failure')

        def create(path, *args, **options):
            if path == self.directory:
                creations.append(path)
            return mkdir(path, *args, **options)

        def write(value, stream, *args, **options):
            if point == 'write' and not faulted:
                stream.write('{')  # A private partial temporary is never published.
                fail()
            return dump(value, stream, *args, **options)

        def sync(fd):
            directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            if point == ('directory_fsync' if directory else 'file_fsync'):
                fail()
            return fsync(fd)

        def publish(source, destination, *args, **options):
            if point == 'publish_before':
                fail()
            result = link(source, destination, *args, **options)
            publications.append(destination)
            if point == 'publish_after':
                fail()  # Readback must resolve a lost publication result.
            return result

        def remove(path, *args, **options):
            if point == 'temporary_cleanup':
                fail()
            return unlink(path, *args, **options)

        with patch.object(Path, 'mkdir', new=create), \
                patch.object(suspension.json, 'dump', side_effect=write), \
                patch.object(suspension.os, 'fsync', side_effect=sync), \
                patch.object(suspension.os, 'link', side_effect=publish), \
                patch.object(suspension.os, 'unlink', side_effect=remove), \
                patch.object(suspension.os, 'replace', side_effect=AssertionError('context overwritten')):
            coordinator = suspension.Coordinator(self.store, self.job, self.anchor)
        self.assertEqual(faulted, [point])
        self.assertEqual(creations, [self.directory])
        self.assertEqual(publications, ['context.json'])
        self.assertEqual(coordinator.channel.read('context.json'), coordinator.context)
        self.assertEqual([path.name for path in self.directory.iterdir()], ['context.json'])
        self.sleep.assert_called_once_with(.25)

    def test_partial_write_recovers_before_one_publication(self):
        self.check_recovery('write')

    def test_file_fsync_recovers_before_one_publication(self):
        self.check_recovery('file_fsync')

    def test_failed_publication_recovers_without_recreating_directory(self):
        self.check_recovery('publish_before')

    def test_lost_publication_result_is_read_back_without_republishing(self):
        self.check_recovery('publish_after')

    def test_directory_fsync_retries_durability_without_republishing(self):
        self.check_recovery('directory_fsync')

    def test_temporary_cleanup_failure_does_not_republish_context(self):
        self.check_recovery('temporary_cleanup')

    def test_foreign_context_appearing_before_publication_is_not_overwritten(self):
        original = os.link
        foreign = None
        def race(source, destination, *args, **options):
            nonlocal foreign
            temporary = self.directory/source
            foreign = json.loads(temporary.read_text())
            foreign['nonce'] = 'a different owner'
            path = self.directory/destination
            path.write_text(json.dumps(foreign))
            path.chmod(0o600)
            return original(source, destination, *args, **options)
        with patch.object(suspension.os, 'link', side_effect=race), \
                self.assertRaises(suspension.OwnershipContradiction):
            suspension.Coordinator(self.store, self.job, self.anchor)
        self.assertEqual(json.loads((self.directory/'context.json').read_text()), foreign)
        self.assertEqual([path.name for path in self.directory.iterdir()], ['context.json'])

    def test_redirected_context_appearing_before_publication_is_refused(self):
        original = os.link
        target = self.store.root/'unrelated-record'
        target.write_text('untouched')
        def race(source, destination, *args, **options):
            (self.directory/destination).symlink_to(target)
            return original(source, destination, *args, **options)
        with patch.object(suspension.os, 'link', side_effect=race), \
                self.assertRaisesRegex(suspension.OwnershipContradiction, 'redirected'):
            suspension.Coordinator(self.store, self.job, self.anchor)
        self.assertEqual(target.read_text(), 'untouched')
        self.assertTrue((self.directory/'context.json').is_symlink())
