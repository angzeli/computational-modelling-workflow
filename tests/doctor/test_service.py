"""Doctor observes exact loaded code and explicitly selected disposable states."""
from contextlib import redirect_stdout, closing
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cmw.cli import main
from cmw.doctor.service import inspect_local, inspect_runtime, format_report
from cmw.doctor.bytecode import _launch_flags, observer_policy
from cmw.jobs.store import Store
from cmw.progress.models import SourceDescriptor


class DoctorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-doctor-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def job(self, text=None, engine='VASP'):
        out = self.root / 'OUTCAR'
        osc = self.root / 'OSZICAR'
        out.write_text('' if text is None else text)
        osc.write_text('')
        store = Store(self.root / 'state')
        job = store.add(argv=['/never/execute', '--version'], cwd=self.root, name='synthetic',
                        engine=engine, hold=True,
                        progress_sources=(SourceDescriptor('outcar', str(out)), SourceDescriptor('oszicar', str(osc))) if engine == 'VASP' else ())
        return store, job

    def test_default_inspection_never_opens_queue_or_connects_or_executes_runner(self):
        runtime = inspect_runtime()
        with patch('cmw.doctor.service.inspect_runtime', return_value=runtime), \
             patch('cmw.jobs.store.Store', side_effect=AssertionError('queue touched')), \
             patch('subprocess.run', side_effect=AssertionError('program launched')):
            result = inspect_local()
        self.assertEqual(result['observation']['status'], 'NOT_REQUESTED')
        self.assertEqual(result['target'], {})
        self.assertEqual(result['scientific_policy'], 'NOT_ASSESSED')
        self.assertEqual(result['worker']['status'], 'UNKNOWN')

    def test_bytecode_policy_reports_effective_flags_separately_from_launch_arguments(self):
        cases = ((['python', '-IB', '-m', 'cmw.cli'], ['-B', '-I']),
                 (['python', '-I', '-B', '/bin/cmw'], ['-B', '-I']),
                 (['python', '-X', '-B', '-Wignore', 'cmw', '-I'], []),
                 (['python', '--check-hash-based-pycs', 'always', '-c', '-IB'], []))
        for argv, flags in cases:
            self.assertEqual(_launch_flags(argv), flags)
        runtime = inspect_runtime()
        policy = runtime['bytecode_policy']
        self.assertEqual(policy['isolated'], sys.flags.isolated)
        self.assertEqual(policy['startup_dont_write_bytecode'], sys.flags.dont_write_bytecode)
        self.assertIs(policy['dont_write_bytecode'], sys.dont_write_bytecode)
        with patch('cmw.doctor.bytecode.sys') as interpreter:
            interpreter.flags.isolated = 1
            interpreter.flags.dont_write_bytecode = 0
            interpreter.dont_write_bytecode = True
            interpreter.orig_argv = ['python', '-I', '-m', 'cmw.cli', '-B']
            sampled = observer_policy(self.root)
        self.assertEqual(sampled['startup_dont_write_bytecode'], 0)
        self.assertIs(sampled['dont_write_bytecode'], True)
        self.assertEqual(sampled['launch_flags'], ['-I'])
        policy.update(sampled, owner_cache_access='WRITABLE')
        with patch('cmw.doctor.service.inspect_runtime', return_value=runtime):
            report = inspect_local()
        warnings = [finding for finding in report['findings'] if finding['code'] == 'BYTECODE_POLICY_WARNING']
        self.assertEqual([finding['severity'] for finding in warnings], ['WARNING'])
        text = format_report(report)
        self.assertIn('owner cache access: WRITABLE', text)
        self.assertIn('sys.flags.dont_write_bytecode=0', text)
        self.assertIn('sys.dont_write_bytecode=True', text)

    def test_cache_access_probes_existing_and_potential_locations_without_writing(self):
        source = self.root / '__init__.py'
        source.touch()
        self.assertEqual(observer_policy(self.root)['owner_cache_access'], 'WRITABLE')
        cache = self.root / '__pycache__'
        self.assertFalse(cache.exists())
        cache.mkdir()
        pyc = cache / 'present.pyc'
        pyc.touch()
        with patch('cmw.doctor.bytecode.os.access', side_effect=lambda path, *a, **k: path == pyc):
            self.assertEqual(observer_policy(self.root)['owner_cache_access'], 'WRITABLE')
        with patch('cmw.doctor.bytecode.os.access', return_value=False):
            self.assertEqual(observer_policy(self.root)['owner_cache_access'], 'READ_ONLY')
        with patch('cmw.doctor.bytecode.os.geteuid', return_value=-1):
            self.assertEqual(observer_policy(self.root)['owner_cache_access'], 'UNKNOWN')
        self.assertEqual(set(cache.iterdir()), {pyc})

    def test_missing_state_does_not_create_anything(self):
        state = self.root / 'absent' / 'state'
        result = inspect_local(state=state)
        self.assertFalse(state.parent.exists())
        self.assertIn('QUEUE_MISSING', [x['code'] for x in result['findings']])
        with self.assertRaises(ValueError):
            inspect_local(job_id='J1.1')

    def test_unknown_schema_fails_without_migration(self):
        store, job = self.job()
        with closing(sqlite3.connect(store.path)) as con, con:
            value = json.loads(con.execute("SELECT value FROM meta WHERE key='control'").fetchone()[0])
            value['schema'] = 999
            con.execute("UPDATE meta SET value=? WHERE key='control'", (json.dumps(value),))
        before = store.path.read_bytes()
        result = inspect_local(store.root, job['display_id'])
        self.assertIn('QUEUE_SCHEMA_UNSUPPORTED', [x['code'] for x in result['findings']])
        self.assertEqual(before, store.path.read_bytes())

    def test_declared_empty_sources_are_pending_not_missing_adapter(self):
        store, job = self.job()
        before = {p.name: p.read_bytes() for p in store.root.iterdir() if p.is_file()}
        result = inspect_local(store.root, job['display_id'])
        codes = [x['code'] for x in result['findings']]
        self.assertIn('SOURCE_PENDING', codes)
        self.assertNotIn('CAPABILITY_UNAVAILABLE', codes)
        self.assertEqual(result['target']['attempt_id'], job['attempt_id'])
        self.assertEqual(result['worker']['status'], 'UNKNOWN')
        self.assertEqual(before, {p.name: p.read_bytes() for p in store.root.iterdir() if p.is_file()})
        self.assertNotIn('argv', json.dumps(result))
        self.assertNotIn('/never/execute', json.dumps(result))

    def test_wrong_attempt_and_schema_wal_are_refused(self):
        store, job = self.job()
        result = inspect_local(store.root, job['id'], expected_attempt_id='not-this-attempt')
        self.assertIn('ATTEMPT_CHANGED', [x['code'] for x in result['findings']])
        with closing(sqlite3.connect(store.path)) as con, con:
            con.execute('PRAGMA journal_mode=WAL')
        before = store.path.read_bytes()
        result = inspect_local(store.root, job['id'])
        self.assertIn('QUEUE_OBSERVATION_UNSAFE', [x['code'] for x in result['findings']])
        self.assertEqual(before, store.path.read_bytes())

    def test_optional_ui_does_not_block_core_or_thermal(self):
        runtime = inspect_runtime()
        runtime['dependencies'] = [dict(d, status='MISSING') if d['name'] in {'textual','rich'} else d
                                   for d in runtime['dependencies']]
        with patch('cmw.doctor.service.inspect_runtime', return_value=runtime):
            core = inspect_local(capability='jobs')
            ui = inspect_local(capability='jobs-ui')
            thermal = inspect_local(capability='thermal')
        self.assertFalse(any(x['severity'] == 'ERROR' for x in core['findings']))
        self.assertTrue(any(x['code'] == 'OPTIONAL_UI_MISSING' and x['severity'] == 'ERROR' for x in ui['findings']))
        self.assertIn('THERMAL_NOT_PROBED', [x['code'] for x in thermal['findings']])
        self.assertFalse(any(x['severity'] == 'ERROR' for x in thermal['findings']))

    def test_missing_adapter_is_runtime_root_cause_not_source_failure(self):
        store, job = self.job()
        with patch('cmw.progress.registry.get_registration', return_value=None):
            result = inspect_local(store.root, job['id'], capability='progress:VASP')
        self.assertIn('CAPABILITY_UNAVAILABLE', [x['code'] for x in result['findings']])
        self.assertNotIn('SOURCE_PENDING', [x['code'] for x in result['findings']])

    def test_lazy_registration_without_adapter_module_is_unavailable(self):
        from cmw.doctor.service import _available
        store, job = self.job()
        def available(name):
            return False if name == 'cmw.progress.vasp' else _available(name)
        with patch('cmw.doctor.service._available', side_effect=available), \
             patch('cmw.progress.service.ObservationService.poll', side_effect=AssertionError('missing adapter polled')):
            result = inspect_local(store.root, job['id'], capability='progress:VASP')
        selected = next(x for x in result['capabilities'] if x['name'] == 'progress:VASP')
        self.assertEqual(selected['status'], 'UNAVAILABLE')
        self.assertNotIn('VASP', result['runtime']['scientific_grammars'])
        self.assertIn('CAPABILITY_UNAVAILABLE', [x['code'] for x in result['findings']])
        self.assertNotIn('PARSER_UNAVAILABLE', [x['code'] for x in result['findings']])

    def test_parser_dependency_failure_is_a_structured_runtime_chain(self):
        store, job = self.job()
        with patch('cmw.jobs.progress.resolve_context', side_effect=ImportError('missing dependency')):
            result = inspect_local(store.root, job['id'])
        issue = next(x for x in result['findings'] if x['code'] == 'PARSER_UNAVAILABLE')
        self.assertEqual(issue['caused_by'], 'DEPENDENCY_MISSING')
        self.assertEqual(result['scientific_policy'], 'NOT_ASSESSED')

    def test_runtime_fingerprint_failure_never_invents_commit(self):
        with patch('cmw.nodes.identity.runtime_identity', side_effect=ValueError('changing')):
            result = inspect_local()
        self.assertIsNone(result['runtime']['identity'])
        self.assertIn('RUNTIME_UNAVAILABLE', [x['code'] for x in result['findings']])
        if result['runtime']['build_source']['dirty']:
            self.assertIsNone(result['runtime']['build_source']['commit'])

    def test_source_only_metadata_is_not_an_installed_origin(self):
        from importlib.metadata import PackageNotFoundError
        with patch('cmw.doctor.service.metadata.distribution', side_effect=PackageNotFoundError):
            runtime = inspect_runtime()
        self.assertEqual(runtime['distribution']['status'], 'UNINSTALLED')
        self.assertEqual(runtime['distribution']['version'], 'uninstalled')
        self.assertIn('cmw-runtime-sha256-v1', runtime['identity']['algorithm'])

    def test_native_unsupported_and_projection_gap_use_separate_domains(self):
        from cmw.progress.models import ProgressSnapshot, MetricSeries, Metric
        store, job = self.job()
        def unsupported(service, context):
            return ProgressSnapshot(context, 'VASP', status='UNSUPPORTED_DIALECT')
        with patch('cmw.progress.service.ObservationService.poll', unsupported):
            result = inspect_local(store.root, job['id'])
        self.assertIn('NATIVE_DIALECT_UNSUPPORTED', [x['code'] for x in result['findings']])
        def gap(service, context):
            return ProgressSnapshot(context, 'VASP', metrics=(MetricSeries(Metric('test','test','test','eV',1.)),))
        with patch('cmw.progress.service.ObservationService.poll', gap):
            result = inspect_local(store.root, job['id'])
        self.assertIn('PROJECTION_MISMATCH', [x['code'] for x in result['findings']])

    def test_actual_native_unsupported_dialect(self):
        store, job = self.job()
        from tests.periodic.vasp.result_fixtures import write_run
        write_run(self.root, settings={'LHFCALC': 'T'})
        result = inspect_local(store.root, job['id'])
        self.assertIn('NATIVE_DIALECT_UNSUPPORTED', [x['code'] for x in result['findings']])

    def test_git_origin_inspection_disables_hooks_and_index_updates(self):
        from cmw.doctor.service import _source_identity
        source = self.root / 'source'
        package = source / 'src' / 'cmw'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('')
        subprocess.run(['/usr/bin/git', 'init', '-q', str(source)], check=True)
        subprocess.run(['/usr/bin/git', '-C', str(source), 'add', '.'], check=True)
        subprocess.run(['/usr/bin/git', '-C', str(source), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture'], check=True)
        marker = self.root / 'hook-ran'
        hook = self.root / 'fsmonitor'
        hook.write_text('#!/bin/sh\ntouch ' + str(marker) + '\n')
        hook.chmod(0o700)
        subprocess.run(['/usr/bin/git', '-C', str(source), 'config', 'core.fsmonitor', str(hook)], check=True)
        index = source / '.git' / 'index'
        before = (index.read_bytes(), index.stat().st_mtime_ns)
        observed = _source_identity(package)
        self.assertEqual(observed['status'], 'EXACT_CHECKOUT')
        self.assertFalse(marker.exists())
        self.assertEqual(before, (index.read_bytes(), index.stat().st_mtime_ns))
        (package / '__init__.py').write_text('dirty = True\n')
        observed = _source_identity(package)
        self.assertEqual(observed['status'], 'DIRTY_SOURCE')
        self.assertIsNone(observed['commit'])

    def test_submission_pin_never_masquerades_as_worker_imports(self):
        store, job = self.job()
        from cmw.jobs.locations import _read_job
        original = _read_job
        def declared(store, identifier):
            item, identity = original(store, identifier)
            item['node_submission'] = {'runtime_id': 'a' * 64}
            return item, identity
        with patch('cmw.jobs.locations._read_job', declared):
            result = inspect_local(store.root, job['id'])
        self.assertEqual(result['worker']['status'], 'UNKNOWN')
        self.assertIsNone(result['worker']['runtime'])
        self.assertIn('WORKER_RUNTIME_MISMATCH', [x['code'] for x in result['findings']])

    def test_cli_version_and_json_origins_are_truthful(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(main(['doctor', '--local', '--json']), 0)
        result = json.loads(stream.getvalue())
        self.assertEqual(result['runtime']['origins']['executable'], sys.executable)
        self.assertIn('cmw-attempt-cleanup/', result['runtime']['protocols']['cleanup_tool'])
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(main(['--version']), 0)
        self.assertTrue(stream.getvalue().startswith('CMW distribution '))
        self.assertNotIn('cleanup', stream.getvalue())
        self.assertIn('read only', format_report(result))

    def test_database_switch_to_wal_at_open_cannot_create_sidecars(self):
        store, job = self.job()
        real_connect = sqlite3.connect
        original = store.path.read_bytes()
        changed = False
        def race(*args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                with closing(real_connect(store.path)) as con, con:
                    con.execute('PRAGMA journal_mode=WAL')
            return real_connect(*args, **kwargs)
        with patch('cmw.jobs.store.sqlite3.connect', side_effect=race):
            result = inspect_local(store.root, job['id'])
        self.assertIn('QUEUE_OBSERVATION_UNSAFE', [x['code'] for x in result['findings']])
        self.assertFalse(Path(str(store.path) + '-shm').exists())
        self.assertFalse(Path(str(store.path) + '-wal').exists())
        self.assertEqual(store.path.read_bytes()[:18], original[:18])

    def test_fixed_commands_grammars_and_all_assets_are_reported(self):
        runtime = inspect_runtime()
        self.assertIn('doctor', runtime['commands'])
        self.assertIn('MULTIWFN', runtime['scientific_grammars'])
        self.assertEqual(len(runtime['assets']), 9)
        self.assertEqual(runtime['protocols']['node'], 1)

    def test_doctor_still_explains_missing_scientific_dependency(self):
        script = """import sys,importlib.abc,json
class MissingASE(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname.split('.')[0]=='ase': raise ModuleNotFoundError('fixture missing ASE')
sys.meta_path.insert(0,MissingASE())
from cmw.cli import main
raise SystemExit(main(['doctor','--json']))
"""
        result = subprocess.run([sys.executable, '-B', '-c', script], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertIn('DEPENDENCY_MISSING', [x['code'] for x in report['findings']])

    def test_core_does_not_import_ui_or_launch_any_program(self):
        script = '''import sys,importlib.abc,subprocess
class NoUI(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname.split('.')[0] in {'textual','rich'}: return None
from cmw.doctor.service import inspect_runtime
runtime=inspect_runtime()
from unittest.mock import patch
with patch('cmw.doctor.service.inspect_runtime',return_value=runtime),patch('subprocess.run',side_effect=AssertionError('spawn')):
 from cmw.cli import main
 main(['doctor','--json'])
assert 'textual' not in sys.modules
'''
        result = subprocess.run([sys.executable, '-B', '-c', script], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
