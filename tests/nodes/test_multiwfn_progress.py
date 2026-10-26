"""Multiwfn endpoint observations keep node identity and strict codec boundaries."""
from copy import deepcopy
import unittest

from cmw.jobs.progress import resolve_context
from cmw.nodes.client import submit
from cmw.nodes.codec import CodecError, decode_typed
from cmw.nodes.protocol import NodeError
from cmw.progress.formatting import progress_text
from cmw.progress.models import ProgressSnapshot, SourceDescriptor
from cmw.progress.service import ObservationService
from tests.nodes import test_backend


class MultiwfnEndpointTests(unittest.TestCase):
    def setUp(self):
        # The shared fixture uses a real private Store and bounded reader, with
        # only host/storage discovery replaced by deterministic test identity.
        self.fixture = test_backend.BackendObservationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.native = self.fixture.root / 'native.log'
        self.native.write_text('Version 3.8\nLoaded explicit-source successfully!\n')
        self.job = self.fixture.store.add(argv=['/bin/true'], cwd=self.fixture.root,
            name='identical analysis name', engine='Multiwfn', hold=True,
            progress_sources=(SourceDescriptor('multiwfn_session', str(self.native), stage='FMO'),))

    def test_real_endpoint_codec_and_local_formatter_agree_without_frontend_reads(self):
        payload = self.fixture.call('progress', self.job)['snapshot']
        remote = decode_typed(payload, ProgressSnapshot)
        service = ObservationService(clock=lambda: 100., monotonic_clock=lambda: 50.)
        service.clock_id = 'fixed-test-observer-clock'
        self.addCleanup(service.close)
        local = service.poll(resolve_context(self.fixture.store, self.job['id']))
        self.assertEqual(remote, local)
        self.assertEqual(remote.context.attempt_id, self.job['attempt_id'])
        self.assertTrue(any(event.kind == 'native' for event in remote.events))
        self.native.unlink()
        # Rendering the received observation must not read a frontend path,
        # including one which has disappeared since the endpoint sampled it.
        for width in (70, 73, 93, 133):
            card = progress_text(remote, width=width, max_lines=8, framed=True, now=100.)
            self.assertEqual(card, progress_text(local, width=width, max_lines=8, framed=True, now=100.))
            self.assertTrue(all(label in card for label in ('ANALYSIS', 'STAGE', 'OUTPUT')))
            self.assertNotIn('Fmax', card)
            self.assertNotIn('SCF', card)

    def test_endpoint_without_adapter_is_explicit_and_does_not_open_sources(self):
        def no_reader(_):
            raise AssertionError('Missing adapter opened a scientific source')
        observer = ObservationService(registration_getter=lambda _: None, reader_factory=no_reader)
        backend = self.fixture.make_backend(observer=observer)
        payload = backend.call('progress', target=backend.target(self.job))['result']['snapshot']
        snapshot = decode_typed(payload, ProgressSnapshot)
        self.assertEqual(snapshot.status, 'ADAPTER_UNAVAILABLE')
        self.assertEqual(snapshot.metrics, ())
        self.assertEqual(snapshot.events, ())
        self.assertEqual(snapshot.card_groups, ())
        self.assertIn('adapter unavailable', progress_text(snapshot).lower())

    def test_incompatible_codec_is_refused_without_partial_reinterpretation(self):
        payload = self.fixture.call('progress', self.job)['snapshot']
        incompatible = deepcopy(payload)
        incompatible['schema'] += 1
        with self.assertRaises(CodecError):
            decode_typed(incompatible, ProgressSnapshot)
        missing = deepcopy(payload)
        del missing['value']['card_groups']
        with self.assertRaises(CodecError):
            decode_typed(missing, ProgressSnapshot)
        invented = deepcopy(payload)
        invented['value']['card_groups'][0]['unrecognized_claim'] = 'finished'
        with self.assertRaises(CodecError):
            decode_typed(invented, ProgressSnapshot)

    def test_missing_or_old_submission_capability_fails_before_spool(self):
        for capabilities in ({}, {'schema': 0, 'engines': ['Multiwfn'], 'target_local_artifacts': 1},
                             {'schema': 1, 'engines': ['ORCA'], 'target_local_artifacts': 1}):
            class OldPeer:
                def call(self, *args, **kwargs):
                    return {'result': {'molecular_submission': capabilities}}
            with self.subTest(capabilities=capabilities), self.assertRaises(NodeError) as caught:
                submit(OldPeer(), {'engine': 'Multiwfn'}, root=self.fixture.root / 'never-spooled')
            self.assertEqual(caught.exception.code, 'INCOMPATIBLE_ENGINE_SUBMISSION')
            self.assertFalse((self.fixture.root / 'never-spooled').exists())


if __name__ == '__main__':
    unittest.main()
