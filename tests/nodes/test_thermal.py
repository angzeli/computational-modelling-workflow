"""Machine-only wire additions retain strict science and owning-node freshness."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from cmw.jobs.cli import machine_text
from cmw.nodes.backend import LocalBackend
from cmw.nodes.codec import CodecError, decode_typed, encode_typed
from cmw.nodes.connections import ConnectedBackend, observed_machine_usage
from cmw.nodes.protocol import NodeError, decode, encode, validate_envelope
from cmw.progress.models import ProgressSnapshot, RunContext


def thermal(value=63.2, age=18.):
    return {'temperature_celsius': value, 'temperature_kind': 'average CPU sensors',
            'source': 'macmon', 'units': 'Celsius', 'sample_timestamp': 100.-age,
            'sample_age': age, 'quality': 'fresh', 'availability_reason': '',
            'freshness_seconds': 30., 'unavailable_seconds': 60.}


def envelope():
    node, queue, attempt = uuid4().hex, uuid4().hex, uuid4().hex
    target = {'node_id': node, 'queue_id': queue, 'job_id': 1, 'attempt_id': attempt}
    return {'schema': 1, 'node_id': node, 'queue_id': queue, 'queue_alias': 'research',
        'session_id': uuid4().hex, 'sequence': 1, 'observed_at': 100., 'clock_id': 'a'*64,
        'runtime': {'fingerprint': 'runtime'}, 'host': {'platform': 'darwin', 'hardware_id': node},
        'result': {'state': {'controller': {}, 'jobs': [{'id': 1, 'attempt_id': attempt,
            'display_id': 'J1.1', 'name': 'never executed', 'status': 'Hold', 'target': target}],
            'machine_usage': {'cpu_percent': 71., 'cpu_quality': 'complete',
                'ram_used_bytes': int(27.1*2**30), 'ram_total_bytes': 36*2**30,
                'memory_quality': 'complete', 'observed_at': 100., 'age_seconds': 0.,
                'thermal': thermal()}}}}


class ThermalWireTests(unittest.TestCase):
    def test_optional_thermal_roundtrip_preserves_unknown_provider_and_legacy_status(self):
        value = envelope()
        for record in (thermal(), {**thermal(), 'source': 'unknown-optional-provider'}, None):
            with self.subTest(record=record):
                value['result']['state']['machine_usage']['thermal'] = record
                self.assertEqual(validate_envelope(decode(encode(value)), 'status'), value)
        del value['result']['state']['machine_usage']['thermal']
        self.assertEqual(validate_envelope(decode(encode(value)), 'status'), value)
        self.assertIn('avg —', machine_text(observed_machine_usage(value, 1000., 1000.)))

    def test_remote_age_uses_node_time_and_elapsed_receipt_not_frontend_wallclock(self):
        value = envelope()
        before = deepcopy(value)
        observed = observed_machine_usage(value, 1000000., 1000004., transport_age=2.)
        self.assertEqual(observed['thermal']['sample_age'], 24.)
        self.assertEqual(observed['thermal']['sample_timestamp'], 82.)
        self.assertEqual(observed['thermal']['quality'], 'fresh')
        self.assertEqual(value, before)
        later = observed_machine_usage(value, 1000000., 1000045.)
        self.assertEqual(later['thermal']['quality'], 'unavailable')
        self.assertIn('avg —', machine_text(later))

    def test_endpoint_capture_delay_and_frontend_elapsed_both_age_temperature(self):
        value = envelope()
        value['observed_at'] = 120.
        value['result']['state']['machine_usage']['thermal'].update(sample_timestamp=100., sample_age=10.)
        observed = observed_machine_usage(value, 1000., 1007., transport_age=3.)
        self.assertEqual(observed['thermal']['sample_age'], 30.)
        self.assertEqual(observed['thermal']['sample_timestamp'], 100.)

    def test_disconnected_temperature_is_stale_then_expires_independently(self):
        value = envelope()
        disconnected = observed_machine_usage(value, 1000., 1001., connected=False)
        self.assertTrue(disconnected['stale'])
        self.assertEqual(disconnected['thermal']['quality'], 'stale')
        self.assertIn('avg 63°C~', machine_text(disconnected))
        expired = observed_machine_usage(value, 1000., 1043., connected=False)
        self.assertEqual(expired['thermal']['quality'], 'unavailable')
        refreshed = envelope()
        refreshed['result']['state']['machine_usage']['thermal'] = thermal(76., 0.)
        fresh = observed_machine_usage(refreshed, 1043., 1043.)
        self.assertEqual(fresh['thermal']['quality'], 'fresh')
        self.assertIn('avg 76°C', machine_text(fresh))
        self.assertEqual(value['result']['state']['machine_usage']['thermal']['temperature_celsius'], 63.2)

    def test_selected_node_cli_qualifies_request_delay_in_text_and_json(self):
        from cmw.nodes.jobs_cli import handle
        value = envelope()
        backend = Mock(alias='MINI', queue='research', node_id=value['node_id'])
        backend.call.return_value = value
        for as_json in (False, True):
            args = SimpleNamespace(state=None, node='MINI', queue=None, nodes_config=None,
                                   jobs_operation='status', json=as_json)
            output = io.StringIO()
            with redirect_stdout(output), patch('cmw.nodes.jobs_cli.connect', return_value=backend), \
                    patch('cmw.nodes.jobs_cli.time.monotonic', side_effect=(1000., 1031.)), \
                    patch('cmw.jobs.cli.status_text', side_effect=lambda state: machine_text(state['machine_usage'])):
                self.assertEqual(handle(args), 0)
            if as_json:
                record = json.loads(output.getvalue())['result']['state']['machine_usage']['thermal']
                self.assertEqual(record['sample_timestamp'], 82.)
                self.assertEqual(record['sample_age'], 49.)
                self.assertEqual(record['quality'], 'stale')
            else:
                self.assertIn('avg 63°C~', output.getvalue())
        self.assertEqual(value['result']['state']['machine_usage']['thermal']['sample_age'], 18.)

    def test_scientific_codec_rejects_thermal_injection(self):
        value = encode_typed(ProgressSnapshot(context=RunContext('test', 'VASP'), adapter='vasp'))
        value['value']['thermal'] = thermal()
        with self.assertRaises(CodecError):
            decode_typed(value)

    def test_wrong_node_runtime_and_late_sequence_do_not_replace_cache(self):
        value = envelope()
        entry = {'transport': 'local', 'config_root': '/unused-test-only', 'node_id': value['node_id'],
            'host_id': 'darwin:' + value['node_id'], 'runtime_id': 'runtime',
            'queues': {'research': value['queue_id']}, 'default_queue': 'research', 'read_only': True}
        for kind in ('node', 'runtime', 'sequence'):
            with self.subTest(kind=kind):
                changed = deepcopy(value)
                changed['sequence'] = 2
                if kind == 'node':
                    changed['node_id'] = uuid4().hex
                elif kind == 'runtime':
                    changed['runtime']['fingerprint'] = 'different-runtime'
                else:
                    changed['sequence'] = 1
                transport = Mock()
                transport.exchange.side_effect = (deepcopy(value), changed)
                backend = ConnectedBackend('MBP', entry, transport=transport)
                accepted = backend.call('status')
                with self.assertRaises(NodeError):
                    backend.call('status')
                self.assertEqual(backend.last_envelope, accepted)
                transport.close.assert_called()

    def test_backend_close_reaps_only_its_sampler_and_observer(self):
        backend = object.__new__(LocalBackend)
        backend._sampler, backend.observer = Mock(), Mock()
        backend._sampler_lock = threading.Lock()
        backend._sessions, backend._validations = {}, {}
        backend.close()
        backend._sampler.close.assert_called_once_with()
        backend.observer.close.assert_called_once_with()

    def test_backend_close_serializes_first_sampler_creation(self):
        backend = object.__new__(LocalBackend)
        backend._closed = False
        backend.config_root = '/unused-test-only'
        backend._sampler_lock = threading.Lock()
        backend._sampler, backend._activity_observer = None, None
        backend._activity_projector = None
        backend.observer, backend.store = Mock(), Mock()
        backend._sessions, backend._validations = {}, {}
        created, proceed = threading.Event(), threading.Event()
        sampler = Mock()
        errors = []
        def make_sampler(**kwargs):
            created.set()
            self.assertTrue(proceed.wait(2))
            return sampler
        def status():
            try:
                backend._status({})
            except Exception as exc:
                errors.append(exc)
        state = {'jobs': [], 'controller': {}}
        with patch('cmw.nodes.thermal.configured_provider') as provider, \
                patch('cmw.jobs.telemetry.Sampler', side_effect=make_sampler), \
                patch('cmw.jobs.activity.project', return_value=state):
            worker = threading.Thread(target=status)
            closer = threading.Thread(target=backend.close)
            worker.start()
            self.assertTrue(created.wait(2))
            closer.start()
            proceed.set()
            worker.join(2)
            closer.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertFalse(errors, errors)
        sampler.close.assert_called_once_with()
        provider.assert_called_once_with('/unused-test-only')
        with patch('cmw.jobs.telemetry.Sampler') as construct, self.assertRaises(NodeError):
            backend._status({})
        construct.assert_not_called()
