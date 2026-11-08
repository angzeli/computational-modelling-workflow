"""Test-only child entrypoint: activity discovery is limited to owned fixtures.

Production never reads fixture-processes.json or selects this launcher. Tests
propagate the replacement launcher to their own controller and worker children.
"""
from contextlib import ExitStack, contextmanager
import json
import importlib
import os
from pathlib import Path
import subprocess
import sys
import stat
import threading
from uuid import uuid4
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_REFUSAL_PID = os.getpid()
_RUNTIME_REFUSAL_CAPTURES = 0
_RUNTIME_REFUSAL_LIMIT = 8
_RUNTIME_REFUSAL_BYTES = 8 * 1024**2


@contextmanager
def record_runtime_refusals():
    """Retain owner-side refusal evidence without repeating a readiness check.

    Only fixture launchers enter this context. Direct cmw CLI endpoints do not.
    An explicit private evidence directory enables at most eight bounded files
    per process; diagnostics never replace the original result or exception.
    """
    from cmw.nodes.backend import LocalBackend
    import cmw

    original = LocalBackend._check
    try:
        runtime_module = importlib.import_module('cmw.nodes.runtime_observation')
    except ModuleNotFoundError as exc:
        if exc.name != 'cmw.nodes.runtime_observation':
            raise
        runtime_module = None
    original_coherence = getattr(runtime_module, '_source_imports_coherent', None)
    coherence = threading.local()

    def observed_coherence(observed):
        current = getattr(coherence, 'current', None)
        try:
            result = original_coherence(observed)
        except BaseException as exc:
            if current is not None:
                current.update(evaluated=True, exception=type(exc).__name__)
            raise
        if current is not None:
            current.update(evaluated=True, result=result)
        return result

    def checked(backend, *args, **kwargs):
        global _RUNTIME_REFUSAL_PID, _RUNTIME_REFUSAL_CAPTURES
        previous = getattr(coherence, 'current', None)
        coherence.current = {'available': callable(original_coherence), 'evaluated': False}
        try:
            return original(backend, *args, **kwargs)
        except Exception as error:
            evidence = os.environ.get('CMW_TEST_EVIDENCE_DIR')
            if (not evidence or getattr(error, 'code', None) != 'RUNTIME_MISMATCH'
                    or getattr(backend, '_test_runtime_refusal_recorded', False)):
                raise
            backend._test_runtime_refusal_recorded = True
            try:
                pid = os.getpid()
                if pid != _RUNTIME_REFUSAL_PID:
                    _RUNTIME_REFUSAL_PID, _RUNTIME_REFUSAL_CAPTURES = pid, 0
                if _RUNTIME_REFUSAL_CAPTURES >= _RUNTIME_REFUSAL_LIMIT:
                    raise ValueError('incomplete capture: eight-file process limit reached')
                _RUNTIME_REFUSAL_CAPTURES += 1
                directory = Path(evidence)
                if not directory.is_absolute() or directory.resolve() != directory:
                    raise ValueError('incomplete capture: evidence directory must be canonical and absolute')
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                info = directory.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) & 0o077):
                    raise ValueError('incomplete capture: evidence directory must be private and owned')
                config = None
                cursor = error.__traceback__
                while cursor is not None:
                    if cursor.tb_frame.f_code is original.__code__:
                        config = cursor.tb_frame.f_locals.get('config')
                        break
                    cursor = cursor.tb_next
                del cursor
                verifier = getattr(backend, '_runtime_verifier', None)
                diagnostic = getattr(backend, 'runtime_diagnostic', None)
                record = {
                    'schema': 1, 'pid': pid, 'python': sys.executable,
                    'interpreter_isolated': bool(sys.flags.isolated),
                    'dont_write_bytecode': sys.dont_write_bytecode,
                    'package_origin': cmw.__file__, 'config_root': str(backend.config_root),
                    'queue_alias': backend.queue_alias,
                    'operation': kwargs.get('operation', args[0] if args else None),
                    'exception': {'type': type(error).__name__, 'code': error.code, 'message': str(error)},
                    'config_source': 'original_check_traceback_local', 'expected_config': config,
                    'runtime_diagnostic': diagnostic,
                    'verifier_observer_state': {
                        'process_started_ns': getattr(verifier, 'process_started_ns', None),
                        'isolated': getattr(verifier, 'isolated', None),
                        'recovered': getattr(verifier, 'recovered', None),
                        'source_imports_coherent': dict(coherence.current)},
                    'verifier_expected': getattr(verifier, 'expected', None),
                    'verifier_original': getattr(verifier, 'original', None),
                    'capture_complete': config is not None and isinstance(diagnostic, dict),
                }
                if not record['capture_complete']:
                    record['capture_error'] = 'Original failed-check config or runtime diagnostic unavailable'
                payload = json.dumps(record, separators=(',', ':'), ensure_ascii=True).encode() + b'\n'
                if len(payload) > _RUNTIME_REFUSAL_BYTES:
                    payload = json.dumps({'schema': 1, 'pid': pid, 'capture_complete': False,
                        'capture_error': 'Original evidence exceeds the 8 MiB capture limit',
                        'original_encoded_bytes': len(payload), 'exception_code': error.code,
                        'category': str((diagnostic or {}).get('category'))[:256]}).encode() + b'\n'
                name = f'runtime-refusal-{pid}-{uuid4().hex}.json'
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                # Exclusive private creation refuses a pre-existing path, including a symlink.
                descriptor = os.open(directory / name, flags, 0o600)
                with os.fdopen(descriptor, 'wb') as handle:
                    handle.write(payload)
            except Exception as capture_error:
                try:
                    print('Runtime refusal diagnostic incomplete: ' + str(capture_error)[:512],
                          file=sys.stderr, flush=True)
                except Exception:
                    pass
            raise
        finally:
            coherence.current = previous

    with ExitStack() as stack:
        stack.enter_context(patch.object(LocalBackend, '_check', checked))
        if callable(original_coherence):
            stack.enter_context(patch.object(runtime_module, '_source_imports_coherent', observed_coherence))
        yield


def environment():
    return {**os.environ, 'PYTHONPATH': os.pathsep.join((str(ROOT), str(ROOT / 'src'))),
            'PYTHONDONTWRITEBYTECODE': '1'}


def fixture_collect(store, state, deadline):
    from cmw.jobs import activity
    from cmw.jobs.ownership import identity
    from cmw.jobs.process_identity import ConfirmedProcessAbsence
    import psutil

    fixtures = store.root / 'fixture-processes.json'
    records = json.loads(fixtures.read_text()) if fixtures.exists() else []
    pids = []
    for record in records:
        try:
            if identity(record['pid']) == record:
                pids.append(record['pid'])
        except (psutil.NoSuchProcess, ConfirmedProcessAbsence):
            pass
    return activity.collect(store, state, deadline, pids_provider=lambda: pids)


class FixtureAdmissionSampler:
    """Deterministic admission evidence, enabled only by this test launcher.

The optional fixture file supplies source-specific CPU/available-memory values
for final-admission races. Production never reads it or selects this class.
"""
    def collect(self, store, *, source='controller', wait=False):
        import time
        from cmw.jobs import activity
        from cmw.jobs.freshness import clock_stamp
        state = store.snapshot()
        state['_telemetry_membership'] = True
        guard = activity.DEFAULT_OBSERVER.scan(store, state, source=source)
        guard.pop('_pids', None)
        path = store.root / 'sharing-admission.json'
        config = json.loads(path.read_text()) if path.exists() else {}
        override = config.get(source, {})
        now = time.time()
        samples = []
        for age in (1, 0):
            observed = now - age
            stamp = clock_stamp(time.monotonic()-age)
            samples.append({'observed_at': observed, 'observed_clock': stamp, 'logical_cpus': 64,
                'machine_usage': {'observed_at': observed, 'observed_clock': stamp, 'stale': False,
                    'cpu_quality': 'fresh', 'memory_quality': 'fresh',
                    'cpu_percent': override.get('cpu_percent', 0),
                    'ram_total_bytes': 64*2**30,
                    'ram_used_bytes': 64*2**30-override.get('available_gib', 63)*2**30},
                'jobs': {}, 'external': {}})
        return guard, {'samples': samples, 'source': source}


def command(arguments):
    """Isolate interpreter imports; explicitly bootstrap only the test support root."""
    return [sys.executable, '-I', '-B', '-c',
            'import sys;sys.path.insert(0,sys.argv.pop(1));from tests.jobs.isolated_runtime import main;main()',
            str(ROOT), *arguments]


def detached(arguments, log):
    from cmw.jobs import runtime
    from cmw.jobs.store import private_opener
    log.parent.mkdir(parents=True, exist_ok=True)
    runtime.reap_detached()
    with open(log, 'ab', buffering=0, opener=private_opener) as output:
        process = subprocess.Popen(
            command(arguments),
            stdin=subprocess.DEVNULL, stdout=output, stderr=output,
            start_new_session=True, close_fds=True, env=environment(), cwd='/')
    runtime._CHILDREN.append(process)
    from tests.fixture_processes import register_process
    register_process(process, role='isolated-runtime ' + str(arguments[0]))
    return process


def install():
    from cmw.jobs import activity, runtime, sharing
    patches = ExitStack()
    patches.enter_context(record_runtime_refusals())
    patches.enter_context(patch.object(activity, 'DEFAULT_OBSERVER', activity.Observer(collector=fixture_collect)))
    patches.enter_context(patch.object(runtime, 'detached', detached))
    patches.enter_context(patch.object(sharing, 'AdmissionSampler', FixtureAdmissionSampler))
    # Node endpoint tests exercise real queue/protocol/control services without
    # scanning unrelated scientific processes for their status presentation.
    from cmw.nodes import endpoint
    from cmw.nodes.backend import LocalBackend
    serve = endpoint.serve
    def isolated_serve(*args, **kwargs):
        kwargs.setdefault('backend_factory', lambda config, queue: LocalBackend(
            config, queue, activity_projector=lambda store: store.snapshot()))
        return serve(*args, **kwargs)
    patches.enter_context(patch.object(endpoint, 'serve', isolated_serve))
    return patches


def main():
    with install():
        if sys.argv[1] == 'cli':
            from cmw.cli import main as cli_main
            del sys.argv[1]
            raise SystemExit(cli_main())
        from cmw.jobs.runtime import main as runtime_main
        from tests.jobs.lifecycle_evidence import trace_runtime
        with trace_runtime(sys.argv[2]):
            runtime_main()


if __name__ == '__main__':
    main()
