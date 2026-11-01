"""Real stdio endpoints with disposable queues; these are not physical nodes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from cmw.jobs.store import Store
from cmw.nodes.connections import ConnectedBackend, save_connection, read_connections, connect
from cmw.nodes.identity import enroll
from cmw.nodes.storage import platform_volume
from cmw.nodes.protocol import NodeError
from cmw.nodes.transport import ProcessTransport


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cmw-endpoints-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def node(self, alias):
        root = self.root / alias
        root.mkdir(mode=0o700)
        storage = root / 'calculation'
        storage.mkdir()
        spec = {'queues': {'research': str(root / 'queue')}, 'storages': {'scratch': str(storage)},
                'profiles': {'benign': {'engine': 'Command', 'runner': '/bin/echo', 'max_cpus': 1}}}
        # Use actual read-only local platform evidence. Unavailable volume identity
        # is a qualification gap, never a reason to enroll by path alone.
        try:
            platform_volume(storage)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.skipTest(f'Local platform identity unavailable for endpoint acceptance: {exc}')
        config = enroll(root / 'node', spec)
        job = Store(root / 'queue').add(argv=['/bin/echo', alias], cwd=storage, name=alias, hold=True)
        command = [sys.executable, '-B', '-m', 'cmw.cli', 'nodes', 'endpoint', '--config', str(root / 'node'), '--queue', 'research']
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'src')}
        channel = ProcessTransport(command, timeout=20, env=env)
        entry = {'transport': 'local', 'config_root': str(root / 'node'), 'node_id': config['node_id'],
                 'host_id': config['host_id'], 'runtime_id': config['runtime']['fingerprint'],
                 'queues': {'research': config['queues']['research']['queue_id']},
                 'default_queue': 'research', 'read_only': True}
        backend = ConnectedBackend(alias, entry, transport=channel)
        self.addCleanup(backend.close)
        return backend, config, job, entry

    def test_two_real_endpoints_keep_same_display_id_and_science_isolated(self):
        left, left_config, left_job, _ = self.node('mbp')
        right, right_config, right_job, _ = self.node('mini')
        self.assertEqual(left_job['display_id'], right_job['display_id'])
        left_status = left.call('status')
        right_status = right.call('status')
        left_target = left_status['result']['state']['jobs'][0]['target']
        right_target = right_status['result']['state']['jobs'][0]['target']
        self.assertNotEqual(left_target, right_target)
        self.assertNotEqual(left_status['session_id'], right_status['session_id'])
        self.assertEqual(left.call('show', target=left_target)['result']['job']['name'], 'mbp')
        self.assertEqual(right.call('show', target=right_target)['result']['job']['name'], 'mini')
        with self.assertRaises(NodeError):
            left.call('locations', target=right_target)
        files = right.call('locations', target=right_target)['result']['locations']
        self.assertTrue(all(item['node_id'] == right_config['node_id'] for item in files))
        self.assertTrue(all(item['local_open_permitted'] is False for item in files))
        progress = right.call('progress', target=right_target)
        self.assertEqual(progress['session_id'], right_status['session_id'])
        self.assertGreater(progress['sequence'], right_status['sequence'])
        right.close()
        state = Store(right_config['queues']['research']['state']).snapshot()
        self.assertEqual(state['jobs'][0]['status'], 'Hold')
        self.assertFalse(state['controller']['dispatch'])

    def test_missing_configuration_read_never_initializes_queue(self):
        root = self.root / 'absent'
        command = [sys.executable, '-B', '-m', 'cmw.cli', 'nodes', 'endpoint', '--config', str(root), '--queue', 'research']
        channel = ProcessTransport(command, env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'src')})
        self.addCleanup(channel.close)
        with self.assertRaises(NodeError):
            channel.exchange('capabilities')
        self.assertFalse(root.exists())

    def test_frontend_registry_clone_unknown_alias_and_runtime_pin(self):
        backend, config, job, entry = self.node('mbp')
        registry = self.root / 'frontend'
        save_connection(registry, 'mbp', entry)
        self.assertEqual(read_connections(registry)['mbp'], entry)
        with self.assertRaises(NodeError):
            save_connection(registry, 'imposter', entry)
        with self.assertRaises(NodeError):
            connect('guessed-mini', root=registry)
        wrong = ConnectedBackend('wrong', {**entry, 'runtime_id': 'different'}, transport=backend.transport)
        with self.assertRaises(NodeError):
            wrong.call('capabilities')
        self.assertIsNone(backend.transport.process)
