"""Private literal configuration is opt-in and cannot execute shell text."""
import os
from pathlib import Path
import tempfile
import unittest

from cmw.jobs.thermal_config import read_settings


class ThermalSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-thermal-settings-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.path = self.root / 'thermal.env'

    def write(self, text):
        self.path.write_text(text)
        self.path.chmod(0o600)

    def test_absent_is_empty_without_creating_any_file(self):
        self.assertEqual(read_settings(self.path), {})
        self.assertFalse(self.path.exists())

    def test_literal_values_preserve_spaces_without_evaluation(self):
        marker = self.root / 'should-not-exist'
        value = '/Applications/path with spaces/$(touch ' + str(marker) + ')/macmon'
        self.write('# thermal only\nCMW_THERMAL_MACMON=' + value + '\nCMW_THERMAL_INTERVAL_SECONDS=20\n')
        self.assertEqual(read_settings(self.path), {'CMW_THERMAL_MACMON': value,
            'CMW_THERMAL_INTERVAL_SECONDS': '20'})
        self.assertFalse(marker.exists())

    def test_unknown_duplicate_export_and_quoted_paths_refused(self):
        for value in ('PATH=/bin', 'export CMW_THERMAL_MACMON=/x',
                      'CMW_THERMAL_MACMON="/x"', 'CMW_THERMAL_MACMON=relative',
                      'CMW_THERMAL_MACMON=/x\nCMW_THERMAL_MACMON=/y',
                      'CMW_THERMAL_MACMON=/x\x00y'):
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaises(ValueError):
                    read_settings(self.path)

    def test_interval_bounds_and_nonfinite_refused(self):
        for value in ('14', '31', 'nan', 'inf', 'garbage', ''):
            with self.subTest(value=value):
                self.write('CMW_THERMAL_INTERVAL_SECONDS=' + value)
                with self.assertRaises(ValueError):
                    read_settings(self.path)
        for value in ('15', '20', '30'):
            self.write('CMW_THERMAL_INTERVAL_SECONDS=' + value)
            self.assertEqual(read_settings(self.path)['CMW_THERMAL_INTERVAL_SECONDS'], value)

    def test_shared_file_and_parent_permissions_refused(self):
        self.write('CMW_THERMAL_MACMON=/x')
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            read_settings(self.path)
        self.path.chmod(0o600)
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):
            read_settings(self.path)
        self.root.chmod(0o700)

    def test_symlink_fifo_oversize_and_invalid_utf8_refused(self):
        target = self.root / 'elsewhere'
        target.write_text('CMW_THERMAL_MACMON=/x')
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            read_settings(self.path)
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        with self.assertRaises(ValueError):
            read_settings(self.path)
        self.path.unlink()
        for raw in (b'x' * 4097, b'\xff'):
            self.path.write_bytes(raw)
            self.path.chmod(0o600)
            with self.assertRaises(ValueError):
                read_settings(self.path)

    def test_relative_and_symlink_parent_refused(self):
        with self.assertRaises(ValueError):
            read_settings('thermal.env')
        actual = self.root / 'actual'
        actual.mkdir(mode=0o700)
        path = actual / 'thermal.env'
        path.write_text('CMW_THERMAL_MACMON=/x')
        path.chmod(0o600)
        link = self.root / 'linked'
        link.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(ValueError):
            read_settings(link / 'thermal.env')
