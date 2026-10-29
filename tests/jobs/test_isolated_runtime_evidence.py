"""No-child controls for the fixture-only owner runtime refusal recorder."""
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from cmw.nodes.backend import LocalBackend
from cmw.nodes.protocol import NodeError
from tests.jobs import isolated_runtime


class RuntimeRefusalEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.destination = self.root / 'evidence'
        self.config = {'node_id': 'original-node', 'runtime': {'fingerprint': 'original'},
                       'runtime_observation': {'members': {'source.py': [1, 2, 3]}}}
        self.diagnostic = {'category': 'security_metadata_changed',
                           'before': {'security': 'original'}, 'after': {'security': 'changed'}}
        self.error = NodeError('RUNTIME_MISMATCH', 'original refusal')
        self.calls = []
        self.enterContext(patch.dict(os.environ, {'CMW_TEST_EVIDENCE_DIR': str(self.destination)}))
        self.enterContext(patch.object(isolated_runtime, '_RUNTIME_REFUSAL_PID', os.getpid()))
        self.enterContext(patch.object(isolated_runtime, '_RUNTIME_REFUSAL_CAPTURES', 0))

    def backend(self):
        backend = object.__new__(LocalBackend)
        backend.config_root = self.root / 'original-node'
        backend.queue_alias = 'research'
        backend.config = {'stale': 'must not be used'}
        backend._load_identity = Mock(side_effect=AssertionError('No extra identity read'))
        backend._runtime_verifier = SimpleNamespace(expected=self.config['runtime'],
                                                    original=self.config['runtime_observation'],
                                                    process_started_ns=123456, isolated=True, recovered=False)
        return backend

    def check(self, backend, operation=None):
        config = self.config  # The recorder must retain this exact failed-check input.
        self.calls.append((backend, operation))
        backend.runtime_diagnostic = self.diagnostic
        raise self.error

    def invoke(self, backend, operation='submit.preview'):
        with patch.object(LocalBackend, '_check', self.check):
            with isolated_runtime.record_runtime_refusals():
                with self.assertRaises(NodeError) as caught:
                    backend._check(operation=operation)
        self.assertIs(caught.exception, self.error)
        backend._load_identity.assert_not_called()

    def test_original_check_exception_and_owner_evidence_are_preserved_once(self):
        backend = self.backend()
        self.invoke(backend)
        self.assertEqual(self.calls, [(backend, 'submit.preview')])
        paths = list(self.destination.glob('runtime-refusal-*.json'))
        self.assertEqual(len(paths), 1)
        record = json.loads(paths[0].read_text())
        self.assertTrue(record['capture_complete'])
        self.assertEqual(record['expected_config'], self.config)
        self.assertEqual(record['runtime_diagnostic'], self.diagnostic)
        self.assertEqual(record['verifier_original'], self.config['runtime_observation'])
        self.assertEqual(record['exception']['code'], self.error.code)
        self.assertEqual(paths[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)
        self.invoke(backend)
        self.assertEqual(len(list(self.destination.glob('*.json'))), 1)
        self.assertEqual(len(self.calls), 2)

    def test_observer_state_and_actual_coherence_result_are_retained_without_recheck(self):
        from cmw.nodes import runtime_observation
        backend = self.backend()
        def check(backend, operation=None):
            config = self.config
            backend.runtime_diagnostic = self.diagnostic
            runtime_observation._source_imports_coherent(self.diagnostic['after'])
            raise self.error
        with patch.object(runtime_observation, '_source_imports_coherent', return_value=False) as coherent, \
             patch.object(LocalBackend, '_check', check), isolated_runtime.record_runtime_refusals():
            with self.assertRaises(NodeError) as caught:
                backend._check('progress')
        self.assertIs(caught.exception, self.error)
        coherent.assert_called_once_with(self.diagnostic['after'])
        record = json.loads(next(self.destination.glob('runtime-refusal-*.json')).read_text())
        self.assertEqual(record['verifier_observer_state'], {'process_started_ns': 123456,
            'isolated': True, 'recovered': False,
            'source_imports_coherent': {'available': True, 'evaluated': True, 'result': False}})
        self.assertEqual(record['expected_config'], self.config)
        backend._load_identity.assert_not_called()

    def test_short_circuited_coherence_is_not_evaluated_by_diagnostics(self):
        from cmw.nodes import runtime_observation
        with patch.object(runtime_observation, '_source_imports_coherent') as coherent:
            self.invoke(self.backend(), 'progress')
        coherent.assert_not_called()
        record = json.loads(next(self.destination.glob('runtime-refusal-*.json')).read_text())
        self.assertEqual(record['verifier_observer_state']['source_imports_coherent'],
                         {'available': True, 'evaluated': False})

    def test_old_runtime_without_observer_module_keeps_original_refusal(self):
        missing = ModuleNotFoundError('old runtime has no observer module', name='cmw.nodes.runtime_observation')
        with patch.object(isolated_runtime.importlib, 'import_module', side_effect=missing):
            self.invoke(self.backend(), 'progress')
        record = json.loads(next(self.destination.glob('runtime-refusal-*.json')).read_text())
        self.assertEqual(record['exception']['message'], 'original refusal')
        self.assertEqual(record['verifier_observer_state']['source_imports_coherent'],
                         {'available': False, 'evaluated': False})

    def test_success_and_disabled_recorder_do_not_create_evidence(self):
        backend = self.backend()
        value = object()
        original = Mock(return_value=value)
        with patch.object(LocalBackend, '_check', original), isolated_runtime.record_runtime_refusals():
            self.assertIs(backend._check('status'), value)
        original.assert_called_once_with(backend, 'status')
        self.assertFalse(self.destination.exists())
        with patch.dict(os.environ, {'CMW_TEST_EVIDENCE_DIR': ''}):
            self.invoke(backend)
        self.assertFalse(self.destination.exists())

    def test_write_error_does_not_mask_original_refusal(self):
        self.destination.mkdir(mode=0o700)
        with patch.object(isolated_runtime.os, 'open', side_effect=OSError('cannot write diagnostic')):
            with redirect_stderr(io.StringIO()) as output:
                self.invoke(self.backend())
        self.assertIn('cannot write diagnostic', output.getvalue())
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertEqual(len(self.calls), 1)

    def test_process_and_byte_limits_are_explicit_incomplete_captures(self):
        with redirect_stderr(io.StringIO()) as output:
            for _ in range(9):
                self.invoke(self.backend())
        self.assertEqual(len(list(self.destination.glob('*.json'))), 8)
        self.assertIn('eight-file process limit', output.getvalue())
        self.assertEqual(len(self.calls), 9)
        with patch.object(isolated_runtime, '_RUNTIME_REFUSAL_CAPTURES', 0):
            self.diagnostic['large'] = 'x' * isolated_runtime._RUNTIME_REFUSAL_BYTES
            self.invoke(self.backend())
        records = [json.loads(path.read_text()) for path in self.destination.glob('*.json')]
        incomplete = [record for record in records if not record['capture_complete']]
        self.assertEqual(len(incomplete), 1)
        self.assertIn('8 MiB', incomplete[0]['capture_error'])
        self.assertTrue(all(path.stat().st_size <= isolated_runtime._RUNTIME_REFUSAL_BYTES
                            for path in self.destination.glob('*.json')))

    def test_noncanonical_or_nonprivate_directory_is_refused_without_masking(self):
        self.destination.mkdir(mode=0o700)
        alias = self.root / 'alias'; alias.symlink_to(self.destination, target_is_directory=True)
        with patch.dict(os.environ, {'CMW_TEST_EVIDENCE_DIR': str(alias)}):
            with redirect_stderr(io.StringIO()) as output:
                self.invoke(self.backend())
        self.assertIn('canonical', output.getvalue())
        self.destination.chmod(0o755)
        with redirect_stderr(io.StringIO()) as output:
            self.invoke(self.backend())
        self.assertIn('private and owned', output.getvalue())
        self.assertEqual(list(self.destination.iterdir()), [])


if __name__ == '__main__':
    unittest.main()


class NativeInterpreterIsolationTests(unittest.TestCase):
    def test_fixture_command_ignores_pythonpath_and_reports_installed_origin(self):
        import subprocess
        import cmw
        argv = isolated_runtime.command([])
        argv[4] = 'import sys,json,cmw;print(json.dumps({"isolated":sys.flags.isolated,"no_bytecode":sys.dont_write_bytecode,"origin":cmw.__file__,"paths":sys.path}))'
        # This probe tests the actual interpreter boundary, without launching Jobs.
        result = subprocess.run(argv[:5], capture_output=True, text=True, check=True,
            env={**os.environ, 'PYTHONPATH':'/definitely-untrusted-cmw-source'}, timeout=10)
        value = json.loads(result.stdout)
        self.assertEqual(value['isolated'], 1)
        self.assertTrue(value['no_bytecode'])
        self.assertNotIn('/definitely-untrusted-cmw-source', value['paths'])
        self.assertEqual(Path(value['origin']).resolve(), Path(cmw.__file__).resolve())
