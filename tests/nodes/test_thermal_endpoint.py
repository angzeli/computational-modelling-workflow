"""Real isolated endpoint acceptance; no research processes or remote machines."""
from pathlib import Path
import tempfile
import unittest

from tests.nodes.thermal_acceptance import thermal_node_surface


class ThermalEndpointTests(unittest.TestCase):
    def test_node_local_temperature_transport_and_disconnect(self):
        with tempfile.TemporaryDirectory(prefix='cmw-thermal-endpoints-') as temporary:
            report = thermal_node_surface(Path(temporary) / 'acceptance')
        self.assertTrue(all(report['checks'].values()))
        self.assertFalse(report['scientific_engine_executed'])
