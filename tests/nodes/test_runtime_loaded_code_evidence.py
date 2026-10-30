"""A policy-qualified late timestamp is separate from import authority."""
from copy import deepcopy
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from cmw.nodes.identity import runtime_observed
from cmw.nodes.runtime_observation import RuntimeRefusal, RuntimeVerifier, _source_imports_coherent
from tests.nodes import test_runtime_observation as fixtures

class LoadedCodeEvidenceTests(unittest.TestCase):
    setUp=fixtures.RuntimeObservationTests.setUp
    touch_metadata=fixtures.RuntimeObservationTests.touch_metadata

    def test_nonisolated_or_incoherent_imports_refuse_without_changing_original(self):
        self.touch_metadata()
        for isolated in (False,True):
            verifier=RuntimeVerifier(self.original,self.epoch,process_started_ns=0,isolated=isolated)
            with patch('cmw.nodes.runtime_observation._source_imports_coherent',return_value=False):
                with self.assertRaises(RuntimeRefusal) as caught:verifier.verify(runtime_observed)
            evidence=caught.exception.evidence
            self.assertEqual(evidence['loaded_code']['failed_predicate'],
                             'source_imports_incoherent' if isolated else 'not_isolated')
            self.assertEqual(verifier.current,self.epoch)
            self.assertTrue(verifier.tainted)

    def test_actual_foreign_import_and_existing_cache_are_incoherent(self):
        for foreign in (True,False):
            cache=self.package/'fixture.pyc';cache.write_bytes(b'not trusted')
            module=SimpleNamespace(__file__='/outside/member.py' if foreign else str(self.member),
                                   __cached__=str(cache))
            with patch.dict(sys.modules,{'cmw.fixture':module},clear=True):
                evidence=[];self.assertFalse(_source_imports_coherent(self.epoch,diagnostics=evidence))
            self.assertEqual(evidence[0]['reason'],
                'outside_observed_members' if foreign else 'non_source_or_cached_import')
