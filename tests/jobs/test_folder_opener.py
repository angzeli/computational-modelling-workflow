from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
import tempfile
import unittest
from unittest.mock import Mock, patch

from cmw.jobs.folder_opener import OpenResult, open_location, request_open
from cmw.jobs.locations import LocationDescriptor, discover
from cmw.jobs.store import Store


class FolderOpenerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw folder 開啟 ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.output = self.root / '中文 $(touch bad); --help'
        self.output.mkdir()
        self.store = Store(self.root/'state')
        self.job = self.store.add(argv=['opaque'], cwd=self.root, name='synthetic')
        self.descriptors = (LocationDescriptor('output', self.output),)

    def selected(self):
        result = discover(self.store, self.job['id'], descriptors=self.descriptors, resolvers=())
        return next(item for item in result.locations if item.key == result.preferred_key)

    def open(self, selected=None, **kwargs):
        return open_location(self.store, selected or self.selected(), descriptors=self.descriptors,
                             resolvers=(), **kwargs)

    def test_safe_exact_argv_unicode_and_shell_characters(self):
        runner = Mock(return_value=CompletedProcess([], 0, '', ''))
        result = self.open(opener=lambda path: request_open(path, platform='darwin', runner=runner))
        self.assertEqual(result.status, 'OPEN_REQUESTED')
        self.assertEqual(runner.call_args.args[0], ['/usr/bin/open', '--', str(self.output)])
        self.assertNotIn('shell', runner.call_args.kwargs)
        self.assertEqual(runner.call_args.kwargs['timeout'], 5)
        self.assertFalse((self.root/'bad').exists())

    def test_source_file_opens_parent_and_growth_does_not_invalidate(self):
        source = self.output/'result.out'
        source.write_text('running')
        self.descriptors = (LocationDescriptor('output', source, kind='file'),)
        selected = self.selected()
        with source.open('a') as handle:
            handle.write(' more output')
        opener = Mock(return_value=OpenResult('OPEN_REQUESTED', 'requested'))
        self.assertEqual(self.open(selected, opener=opener).status, 'OPEN_REQUESTED')
        opener.assert_called_once_with(str(self.output))

    def test_missing_preferred_never_falls_back_to_inputs(self):
        selected = self.selected()
        self.output.rmdir()
        opener = Mock()
        self.assertEqual(self.open(selected, opener=opener).status, 'NOT_FOUND')
        opener.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_symlink_redirection_rejects_old_selection(self):
        link = self.root/'linked'
        link.symlink_to(self.output, target_is_directory=True)
        self.descriptors = (LocationDescriptor('output', link),)
        selected = self.selected()
        other = self.root/'other'
        other.mkdir()
        link.unlink()
        link.symlink_to(other, target_is_directory=True)
        opener = Mock()
        self.assertEqual(self.open(selected, opener=opener).status, 'LOCATION_CHANGED')
        opener.assert_not_called()

    def test_replaced_directory_and_state_identity_rejected(self):
        selected = self.selected()
        self.output.rename(self.root/'saved-output')
        self.output.mkdir()
        opener = Mock()
        self.assertEqual(self.open(selected, opener=opener).status, 'LOCATION_CHANGED')
        opener.assert_not_called()
        other = Store(self.root/'another-state')
        self.assertEqual(open_location(other, selected, opener=opener).status, 'LOCATION_CHANGED')

    def test_changed_attempt_or_declaration_rejected(self):
        selected = self.selected()
        self.descriptors = (LocationDescriptor('output', self.root),)
        opener = Mock()
        self.assertEqual(self.open(selected, opener=opener).status, 'LOCATION_CHANGED')
        self.descriptors = (LocationDescriptor('output', self.output),)
        with self.store.transaction() as con:
            job = Store.get(con, self.job['id'])
            job['attempt_id'] = 'changed'
            Store.save(con, job)
        self.assertEqual(self.open(selected, opener=opener).status, 'LOCATION_CHANGED')
        opener.assert_not_called()

    def test_no_desktop_and_failed_opener_preserve_path(self):
        runner = Mock()
        result = request_open(self.output, platform='linux', runner=runner)
        self.assertEqual(result.status, 'UNSUPPORTED_DESKTOP')
        self.assertEqual(result.path, str(self.output))
        runner.assert_not_called()
        runner.return_value = CompletedProcess([], 1, '', 'Finder request refused')
        self.assertEqual(request_open(self.output, platform='darwin', runner=runner).status, 'OPEN_FAILED')
        runner.side_effect = TimeoutExpired('open', 5)
        self.assertEqual(request_open(self.output, platform='darwin', runner=runner).status, 'OPEN_FAILED')

    def test_executable_directory_bundle_is_never_dispatched(self):
        for suffix in ('.app', '.workflow', '.scptd', '.pkg', '.mpkg'):
            application = self.root / ('Do Not Launch' + suffix)
            application.mkdir()
            runner = Mock()
            result = request_open(application, platform='darwin', runner=runner)
            self.assertEqual(result.status, 'LOCATION_UNKNOWN')
            runner.assert_not_called()

    def test_direct_request_never_opens_file_or_uri(self):
        script = self.root/'malicious.sh'
        script.write_text('touch should-not-execute')
        runner = Mock()
        for path in (script, 'https://example.invalid', 'relative-file'):
            self.assertEqual(request_open(path, platform='darwin', runner=runner).status, 'LOCATION_UNKNOWN')
        runner.assert_not_called()

    def test_redirect_at_platform_opener_boundary_is_rejected(self):
        link = self.root / 'redirected-platform-target'
        link.symlink_to(self.output, target_is_directory=True)
        runner = Mock()
        result = request_open(link, platform='darwin', runner=runner)
        self.assertEqual(result.status, 'LOCATION_CHANGED')
        runner.assert_not_called()

    def test_invalid_opener_return_is_not_success(self):
        self.assertEqual(self.open(opener=lambda path: None).status, 'OPEN_FAILED')

    def test_recheck_immediately_before_opener_rejects_redirect(self):
        selected = self.selected()
        opener = Mock()
        with patch('cmw.jobs.folder_opener.inspect_path', return_value=(str(self.root), 'AVAILABLE',
                   selected.target_identity, selected.directory_identity, '')):
            result = self.open(selected, opener=opener)
        self.assertEqual(result.status, 'LOCATION_CHANGED')
        opener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
