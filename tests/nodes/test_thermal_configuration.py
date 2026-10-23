"""Endpoint opt-in is local configuration, not client environment authority."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from cmw.nodes.thermal import configured_provider
from cmw.nodes.transport import ssh_command


class EndpointThermalConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-node-thermal-settings-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)

    def test_endpoint_file_overrides_frontend_environment(self):
        config = self.root / 'thermal.env'
        config.write_text('CMW_THERMAL_MACMON=/Applications/endpoint/macmon\nCMW_THERMAL_INTERVAL_SECONDS=25\n')
        config.chmod(0o600)
        factory = Mock()
        with patch.dict(os.environ, {'CMW_THERMAL_MACMON': '/frontend/macmon',
                                     'CMW_THERMAL_INTERVAL_SECONDS': '15'}):
            self.assertIs(configured_provider(self.root, provider_factory=factory), factory.return_value)
        factory.assert_called_once_with('/Applications/endpoint/macmon', interval=25.)

    def test_missing_endpoint_file_does_not_inherit_frontend_opt_in(self):
        factory = Mock()
        with patch.dict(os.environ, {'CMW_THERMAL_MACMON': '/frontend/macmon'}):
            configured_provider(self.root, provider_factory=factory)
        factory.assert_called_once_with(None, interval=20.)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_configuration_is_monitoring_only_and_reopen_recovers(self):
        config = self.root / 'thermal.env'
        config.write_text('PATH=/should/not/be/accepted\n')
        config.chmod(0o600)
        factory = Mock()
        provider = configured_provider(self.root, provider_factory=factory)
        try:
            snapshot = provider.snapshot()
            self.assertEqual(snapshot['provider_status'], 'invalid_configuration')
            self.assertEqual(snapshot['quality'], 'unavailable')
            self.assertIn('Endpoint thermal configuration invalid', snapshot['availability_reason'])
            self.assertIsNone(provider.thread)
            factory.assert_not_called()
        finally:
            provider.close()
        config.write_text('CMW_THERMAL_MACMON=/Applications/endpoint/macmon\n')
        configured_provider(self.root, provider_factory=factory)
        factory.assert_called_once_with('/Applications/endpoint/macmon', interval=20.)

    def test_fixed_ssh_bootstrap_does_not_forward_thermal_settings(self):
        with patch.dict(os.environ, {'CMW_THERMAL_MACMON': '/frontend/macmon'}):
            argv = ssh_command('mini-test', '/endpoint/cmw', '/endpoint/node', 'research')
        self.assertNotIn('/frontend/macmon', ' '.join(argv))
        self.assertNotIn('SendEnv', ' '.join(argv))
        self.assertEqual(argv[-1], '/endpoint/cmw nodes endpoint --config /endpoint/node --queue research')
