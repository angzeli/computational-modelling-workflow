"""Loaded-code refusal identifies its actual short-circuited predicate."""
from copy import deepcopy
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cmw.nodes.runtime_observation import RuntimeRefusal, RuntimeVerifier, _source_imports_coherent
from tests.nodes import test_runtime_observation as fixtures


class LoadedCodeEvidenceTests(unittest.TestCase):
    setUp = fixtures.RuntimeObservationTests.setUp
    touch_metadata = fixtures.RuntimeObservationTests.touch_metadata

    def test_old_observer_records_actual_changed_members_and_birth_comparison(self):
        from cmw.nodes.identity import runtime_observed
        original = deepcopy(self.epoch)
        verifier = RuntimeVerifier(self.original, self.epoch, process_started_ns=0, isolated=True)
        self.touch_metadata()
        with self.assertRaises(RuntimeRefusal) as caught:
            verifier.verify(runtime_observed)
        evidence = caught.exception.evidence
        self.assertEqual(evidence['loaded_code']['failed_predicate'], 'metadata_not_before_process_start')
        self.assertEqual(evidence['loaded_code']['changed_members'], ['__init__.py'])
        self.assertEqual(evidence['loaded_code']['latest_changed_ctime_ns'], evidence['after']['members']['__init__.py'][4])
        self.assertIsNone(evidence['loaded_code']['source_imports_coherent'])
        self.assertEqual(evidence['before'], original)
        self.assertEqual(evidence['actual_digest'], evidence['expected_digest'])
        self.assertEqual(verifier.current, original)
        self.assertFalse(verifier.recovered)

    def test_import_predicate_is_separate_from_birth_and_records_offending_module(self):
        from cmw.nodes.identity import runtime_observed
        self.touch_metadata()
        verifier = RuntimeVerifier(self.original, self.epoch, process_started_ns=10**20, isolated=True)
        with patch('cmw.nodes.runtime_observation._source_imports_coherent', return_value=False):
            with self.assertRaises(RuntimeRefusal) as caught:
                verifier.verify(runtime_observed)
        self.assertEqual(caught.exception.evidence['loaded_code']['failed_predicate'], 'source_imports_incoherent')
        self.assertFalse(caught.exception.evidence['loaded_code']['source_imports_coherent'])
        # Inspect one independent foreign import with the real helper, using recorded paths only.
        with patch.dict(sys.modules, {'cmw.bad_fixture': SimpleNamespace(__file__='/outside/member.py')}, clear=True):
            details=[]
            self.assertFalse(_source_imports_coherent(self.epoch, diagnostics=details))
        self.assertEqual(details, [{'module':'cmw.bad_fixture', 'path':'/outside/member.py',
                                   'reason':'outside_observed_members'}])
