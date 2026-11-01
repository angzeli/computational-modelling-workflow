"""Compose existing identity and observer contracts without acquiring execution authority."""
from __future__ import annotations

from importlib import metadata, util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time


FINDING_GUIDANCE = {
    'RUNTIME_UNAVAILABLE': 'Inspect the reported package origin and interpreter; retry after package files stop changing.',
    'DISTRIBUTION_UNBOUND': 'Compare the loaded package origin with the selected installation metadata.',
    'SOURCE_BUILD_UNKNOWN': 'Use the implementation fingerprint; no verified build commit is available.',
    'SOURCE_BUILD_DIRTY': 'Review changes in this exact source checkout before building a new isolated candidate.',
    'DEPENDENCY_MISSING': 'Inspect the named dependency in this interpreter; do not repair an active runtime in place.',
    'OPTIONAL_UI_MISSING': 'Use the core doctor JSON view; inspect the selected installation jobs extra separately.',
    'ASSET_MISSING': 'Inspect the selected package runner assets; do not execute or substitute a runner.',
    'CAPABILITY_UNAVAILABLE': 'Compare this loaded implementation with the qualified candidate; check queue compatibility before any switch.',
    'THERMAL_NOT_PROBED': 'Inspect optional thermal configuration separately; temperature availability does not determine calculation readiness.',
    'QUEUE_MISSING': 'Check the explicit state location; observation does not initialize a queue.',
    'QUEUE_SCHEMA_UNSUPPORTED': 'Inspect the queue with its recorded compatible runtime; do not migrate or point an older worker at it.',
    'QUEUE_UNAVAILABLE': 'Check the selected state location and access using its owning runtime.',
    'QUEUE_OBSERVATION_UNSAFE': 'Use the existing owner observation endpoint; do not repair database journals during diagnosis.',
    'JOB_UNAVAILABLE': 'Read the selected queue job list and select an existing immutable attempt.',
    'ATTEMPT_CHANGED': 'Reselect the exact job attempt; do not reuse evidence from its predecessor.',
    'WORKER_RUNTIME_UNKNOWN': 'Retain the existing worker and inspect original launch provenance; a current launcher cannot establish worker imports.',
    'WORKER_RUNTIME_MISMATCH': 'Compare recorded producer provenance with this observer; keep the active worker under its original runtime.',
    'SOURCE_BINDING_UNRESOLVED': 'Open Sources for this exact attempt and inspect declarations without activating changes.',
    'SOURCE_BINDING_CONFLICT': 'Inspect conflicting source declarations for this attempt; do not choose a source automatically.',
    'SOURCE_PENDING': 'Inspect the declared source health later; an empty or future file does not mean a missing adapter.',
    'SOURCE_UNAVAILABLE': 'Inspect the declared source on its owning node; do not replace its binding.',
    'NATIVE_DIALECT_UNSUPPORTED': 'Compare the observed dialect with the adapter contract; no scientific conclusion is available.',
    'PARSER_UNAVAILABLE': 'Inspect runtime and adapter findings before investigating native source content.',
    'PROGRESS_INCOMPLETE': 'Use bounded progress history or a fresh snapshot for the same attempt; prefix coverage is not full replay.',
    'PROJECTION_MISMATCH': 'Compare the same frozen native metric and card projection in progress diagnostics.',
    'SCIENTIFIC_POLICY_NOT_ASSESSED': 'Use the existing scientific validation contract with an explicit policy when separately requested.',
}
CAPABILITIES = ('jobs', 'jobs-ui', 'nodes', 'progress:VASP', 'progress:ORCA',
                'progress:MULTIWFN', 'thermal', 'vasp-runner')
QUALIFICATION = ('Read-only observation, not execution readiness or scientific acceptance. '
                 'Component observations are bounded and not an atomic snapshot.')


def finding(code, domain, *, scope='local', severity='WARNING', summary=None,
            evidence=None, capability=None, caused_by=None):
    return {'code': code, 'domain': domain, 'scope': scope, 'severity': severity,
            'summary': summary or code.replace('_', ' ').capitalize(),
            'evidence': evidence or {}, 'next_check': FINDING_GUIDANCE.get(code,
                'Inspect the selected owner diagnostic without changing runtime or state.'),
            'capability': capability, 'caused_by': caused_by}


def _available(module):
    try:
        return util.find_spec(module) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _adapter_available(engine):
    from cmw.progress.registry import get_registration
    modules = {'VASP': 'cmw.progress.vasp', 'ORCA': 'cmw.progress.orca',
               'MULTIWFN': 'cmw.progress.multiwfn'}
    return (get_registration(engine) is not None
            and (engine not in modules or _available(modules[engine])))


def _source_identity(package):
    # Git is consulted only for the checkout which actually supplies this package.
    root = package.parent.parent
    unknown = {'status': 'UNKNOWN', 'commit': None, 'dirty': None, 'root': None}
    if package.parent.name != 'src' or not (root / '.git').exists():
        return unknown
    try:
        commands = (['rev-parse', '--show-toplevel'], ['rev-parse', 'HEAD'],
                    ['status', '--porcelain', '--untracked-files=normal', '--ignore-submodules=all'])
        results = [subprocess.run(['/usr/bin/git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false', '-C', str(root), *args], check=True,
                                  capture_output=True, text=True, timeout=3).stdout.strip()
                   for args in commands]
        if Path(results[0]).resolve() != root:
            return unknown
        dirty = bool(results[2])
        return {'status': 'DIRTY_SOURCE' if dirty else 'EXACT_CHECKOUT',
                'commit': None if dirty else results[1], 'dirty': dirty, 'root': str(root)}
    except (OSError, subprocess.SubprocessError):
        return unknown


def distribution_version():
    try:
        return metadata.version('computational-modelling-workflow')
    except metadata.PackageNotFoundError:
        return 'uninstalled'


def inspect_runtime():
    """Report the actual loaded package; never probe scientific executables."""
    import cmw
    from cmw.core.attempt_cleanup import CLEANUP_TOOL_VERSION
    from cmw.nodes.identity import runtime_identity
    from cmw.jobs.runner_assets import _CLOSURES
    package = Path(cmw.__file__).resolve().parent
    distribution = {'name': 'computational-modelling-workflow',
                    'version': distribution_version(), 'status': 'UNBOUND'}
    try:
        dist = metadata.distribution(distribution['name'])
        origin = Path(dist.locate_file('cmw/__init__.py')).resolve()
        if origin == Path(cmw.__file__).resolve():
            distribution['status'] = 'MATCHED'
    except metadata.PackageNotFoundError:
        distribution['status'] = 'UNINSTALLED'
    try:
        identity, error = runtime_identity(), None
    except (OSError, ValueError, RuntimeError) as exc:
        identity, error = None, type(exc).__name__
    dependencies = []
    for name, module, required in (('ase', 'ase', 'scientific preparation'),
            ('gemmi', 'gemmi', 'structure import'), ('numpy', 'numpy', 'scientific adapters'),
            ('PyYAML', 'yaml', 'configuration'), ('psutil', 'psutil', 'jobs'),
            ('textual', 'textual', 'jobs-ui'), ('rich', 'rich', 'jobs-ui')):
        try:
            version = metadata.version(name)
        except metadata.PackageNotFoundError:
            version = None
        dependencies.append({'name': name, 'version': version, 'required_for': required,
                             'status': 'AVAILABLE' if _available(module) else 'MISSING'})
    assets = []
    asset_names = sorted({'runtime_python.sh', *(name for closure in _CLOSURES.values() for name in closure)})
    asset_root = package / '_runner_assets' / 'scripts'
    if not asset_root.is_dir() and package.parent.name == 'src':
        asset_root = package.parent.parent / 'scripts'
    for name in asset_names:
        path = asset_root / name
        assets.append({'name': name, 'status': 'AVAILABLE' if path.is_file() else 'MISSING', 'path': str(path)})
    from cmw.progress.registry import get_registration
    from cmw.cli import COMMAND_MODULES
    commands = sorted(name for name, module in COMMAND_MODULES.items() if (package / module).is_file())
    grammar_contracts = {'VASP': '6.6.1 ordinary static/fixed-cell; bounded DAV/RMM and force tables',
        'ORCA': '6.1.x demonstrated DIIS/SOSCF and labelled geometry tables',
        'MULTIWFN': '2026.7.15 FMO/ESP/IGMH/IFCT bounded native and sequenced runner markers'}
    grammars = {engine: contract for engine, contract in grammar_contracts.items() if _adapter_available(engine)}
    return {'identity': identity, 'identity_error': error, 'distribution': distribution,
            'origins': {'package': str(package), 'cli': str(package / 'cli.py'),
                        'executable': sys.executable},
            'build_source': _source_identity(package), 'dependencies': dependencies, 'assets': assets,
            'commands': commands, 'scientific_grammars': grammars,
            'protocols': {'node': 1, 'jobs': [1, 2, 3, 4], 'doctor': 1,
                          'cleanup_tool': CLEANUP_TOOL_VERSION}}


def _capabilities(runtime):
    from cmw.progress.registry import get_registration
    result = []
    missing = {d['name'] for d in runtime['dependencies'] if d['status'] == 'MISSING'}
    for name in CAPABILITIES:
        status = 'AVAILABLE'
        if name == 'jobs-ui' and missing.intersection({'textual', 'rich'}):
            status = 'UNAVAILABLE'
        if name.startswith('progress:') and not _adapter_available(name.split(':')[1]):
            status = 'UNAVAILABLE'
        result.append({'name': name, 'status': status,
                       'qualification': 'Implementation present; configuration and scientific grammar are separate.'})
    return result


def _queue_control(store):
    """Reuse the existing safe read lock and schema rules, without migration."""
    with store.read_transaction() as con:
        raw = con.execute("SELECT value FROM meta WHERE key='control'").fetchone()
        if raw is None or len(raw[0]) > 65536:
            raise ValueError('Queue control record is unavailable or oversized')
        schema = json.loads(raw[0]).get('schema')
        control = store.control(con)
        identity = store._queue_identity(con, schema) if schema == 4 else None
        return schema, control, identity


def _observe(report, store, job):
    from cmw.jobs.progress import resolve_context
    from cmw.jobs.locations import _read_job
    from cmw.jobs.snapshot import capture_snapshot
    from cmw.progress.service import ObservationService
    from cmw.progress.registry import get_registration
    context = resolve_context(store, job['id'], expected_attempt_id=job['attempt_id'])
    binding = {'status': context.binding_status, 'binding_id': context.binding_id,
               'generation': context.binding_generation, 'source_count': len(context.sources),
               'declared_count': len(context.declarations)}
    report['observation']['binding'] = binding
    if not _adapter_available(context.engine):
        report['findings'].append(finding('CAPABILITY_UNAVAILABLE', 'runtime', scope='job',
            severity='ERROR', capability='progress:' + context.engine))
        return
    if context.binding_status != 'BOUND':
        code = 'SOURCE_BINDING_CONFLICT' if context.binding_status == 'SOURCE_CONFLICT' else 'SOURCE_BINDING_UNRESOLVED'
        report['findings'].append(finding(code, 'binding', scope='job', evidence=binding,
                                          capability='progress:' + context.engine))
    service = ObservationService(max_contexts=1, max_inflight=1)
    try:
        snapshot = service.poll(context)  # Existing 8-source / 64-KiB-per-source bound; no full replay.
    finally:
        service.close()
    current, _ = _read_job(store, job['id'])
    if current['attempt_id'] != job['attempt_id']:
        raise ValueError('ATTEMPT_CHANGED')
    frozen = capture_snapshot(job, progress=snapshot,
        runtime=(report['runtime'].get('identity') or {})).to_dict()['progress']
    # Native text, coordinates, complete metrics and arbitrary diagnostic messages
    # do not belong in the default doctor record or capsule.
    progress = {key: frozen.get(key) for key in ('adapter', 'status', 'completeness', 'read_health',
        'last_observation_at', 'last_new_evidence_at', 'last_source_change_at')}
    progress['sources'] = [{key: item.get(key) for key in ('role', 'path', 'generation', 'health',
        'offset', 'has_more', 'bytes_read', 'last_success_at', 'last_change_at', 'identity')}
        for item in frozen['sources']]
    progress['source_diagnostics'] = [{key: item.get(key) for key in ('role', 'status', 'parsed_lines', 'complete_rows', 'usable_rows', 'last_complete_row_at')} for item in frozen['source_diagnostics']]
    progress['metric_ids'] = [item.latest.semantic_id for item in snapshot.metrics][:64]
    progress['projection_group_count'] = len(snapshot.card_groups)
    report['observation']['progress'] = progress
    for source in progress['sources']:
        code = ('SOURCE_PENDING' if source['health'] in {'PENDING', 'EMPTY', 'MISSING', 'NOT_CREATED'} or (source['health'] == 'OK' and source['offset'] == 0)
                else 'SOURCE_UNAVAILABLE' if source['health'] not in {'OK', 'UNCHANGED'} else None)
        if code:
            report['findings'].append(finding(code, 'storage', scope='job', evidence=source,
                capability='progress:' + context.engine))
    if snapshot.status in {'UNSUPPORTED', 'UNSUPPORTED_DIALECT', 'UNSUPPORTED_MODE'} or any(item.status == 'UNSUPPORTED' for item in snapshot.source_diagnostics):
        report['findings'].append(finding('NATIVE_DIALECT_UNSUPPORTED', 'parser', scope='job',
            evidence={'adapter': snapshot.adapter, 'status': snapshot.status}))
    elif snapshot.status in {'ADAPTER_ERROR', 'ADAPTER_UNAVAILABLE'}:
        report['findings'].append(finding('PARSER_UNAVAILABLE', 'parser', scope='job',
            severity='ERROR', evidence={'status': snapshot.status}))
    if any(source['has_more'] for source in progress['sources']):
        report['findings'].append(finding('PROGRESS_INCOMPLETE', 'parser', scope='job', severity='INFO'))
    registration = get_registration(context.engine)
    if snapshot.metrics and registration.card_groups and not snapshot.card_groups:
        report['findings'].append(finding('PROJECTION_MISMATCH', 'projection', scope='job', severity='ERROR',
            evidence={'metric_count': len(snapshot.metrics), 'group_count': 0}))


def inspect_local(state=None, job_id=None, expected_attempt_id=None, capability=None):
    """Local defaults inspect only CMW; state and sources require explicit selection."""
    if job_id is not None and state is None:
        raise ValueError('A selected job requires an explicit state or owning node')
    if capability is not None and capability not in CAPABILITIES:
        raise ValueError('Unknown doctor capability: ' + str(capability))
    now = time.time()
    runtime = inspect_runtime()
    scope = 'job' if job_id is not None else 'local'
    report = {'schema': 1, 'scope': scope, 'captured_at': now, 'qualification': QUALIFICATION,
        'runtime': runtime, 'endpoint_runtime': None,
        'worker': {'status': 'UNKNOWN', 'runtime': None,
                   'qualification': 'Current launcher and observer imports do not identify an existing worker.'},
        'target': {}, 'selected_capability': capability, 'capabilities': _capabilities(runtime), 'findings': [],
        'observation': {'status': 'NOT_REQUESTED', 'observed_at': None,
            'coverage': 'One bounded poll of explicitly selected sources; no log corpus or full replay.',
            'transport': {'status': 'LOCAL'}, 'progress': None, 'binding': None, 'controller': None},
        'scientific_policy': 'NOT_ASSESSED'}
    add = report['findings'].append
    if runtime['identity'] is None:
        add(finding('RUNTIME_UNAVAILABLE', 'runtime', severity='ERROR'))
    if runtime['distribution']['status'] != 'MATCHED':
        add(finding('DISTRIBUTION_UNBOUND', 'runtime', severity='INFO', evidence=runtime['distribution']))
    if runtime['build_source']['status'] != 'EXACT_CHECKOUT':
        add(finding('SOURCE_BUILD_DIRTY' if runtime['build_source']['dirty'] else 'SOURCE_BUILD_UNKNOWN',
                    'runtime', severity='INFO'))
    for dep in runtime['dependencies']:
        if dep['status'] != 'MISSING':
            continue
        optional = dep['name'] in {'textual', 'rich'}
        add(finding('OPTIONAL_UI_MISSING' if optional else 'DEPENDENCY_MISSING', 'runtime',
                    severity='INFO' if optional and capability != 'jobs-ui' else 'ERROR',
                    evidence=dep, capability=dep['required_for']))
    for asset in runtime['assets']:
        if asset['status'] == 'MISSING':
            add(finding('ASSET_MISSING', 'runtime', evidence=asset, capability='jobs'))
    if capability == 'thermal':
        add(finding('THERMAL_NOT_PROBED', 'runtime', severity='INFO', capability='thermal'))
    if capability:
        selected = next(item for item in report['capabilities'] if item['name'] == capability)
        if selected['status'] != 'AVAILABLE':
            add(finding('CAPABILITY_UNAVAILABLE', 'runtime', severity='ERROR', capability=capability,
                caused_by='OPTIONAL_UI_MISSING' if capability == 'jobs-ui' else None))
    if state is not None:
        from cmw.jobs.store import Store, JobsError
        store = Store(state)
        report['target'] = {'state': str(store.root)}
        try:
            schema, control, identity = _queue_control(store)
            report['target']['queue_schema'] = schema
            if identity:
                report['target'].update({key: identity[key] for key in ('node_id', 'queue_id')})
            report['observation'].update(status='OBSERVED', observed_at=time.time())
            report['observation']['controller'] = {'dispatch': control.get('dispatch'),
                'heartbeat': control.get('heartbeat'), 'owner_recorded': bool(control.get('owner')),
                'ownership': 'NOT_PROBED'}
        except (OSError, ValueError, JobsError, sqlite3.Error, KeyError, TypeError) as exc:
            code = ('QUEUE_MISSING' if isinstance(exc, FileNotFoundError) else
                    'QUEUE_SCHEMA_UNSUPPORTED' if 'Unsupported Jobs state schema' in str(exc) else
                    'QUEUE_OBSERVATION_UNSAFE' if 'journal format' in str(exc) else 'QUEUE_UNAVAILABLE')
            add(finding(code, 'storage', severity='ERROR', scope=scope, evidence={'error_type': type(exc).__name__}))
            report['observation']['status'] = 'UNAVAILABLE'
            return report
        if job_id is not None:
            from cmw.jobs.locations import _read_job
            try:
                job, _ = _read_job(store, job_id)
                if expected_attempt_id is not None and job['attempt_id'] != expected_attempt_id:
                    raise ValueError('ATTEMPT_CHANGED')
                report['target'].update({key: job[key] for key in ('attempt_id', 'engine', 'status')})
                report['target']['job_id'] = job['id']
                add(finding('WORKER_RUNTIME_UNKNOWN', 'runtime', scope='job', severity='INFO'))
                declared = (job.get('node_submission') or {}).get('runtime_id')
                if declared:
                    report['worker']['expected_runtime_id'] = declared
                    report['worker']['qualification'] += ' Submission runtime is an expectation, not observed worker imports.'
                    actual = (runtime['identity'] or {}).get('fingerprint')
                    if actual and declared != actual:
                        add(finding('WORKER_RUNTIME_MISMATCH', 'runtime', scope='job',
                            evidence={'declared_runtime': declared, 'observer_runtime': actual},
                            summary='Recorded submission runtime differs; actual worker imports remain unknown.'))
                _observe(report, store, job)
            except (OSError, ValueError, JobsError, KeyError, TypeError, sqlite3.Error, ImportError) as exc:
                code = ('PARSER_UNAVAILABLE' if isinstance(exc, ImportError) else
                        'ATTEMPT_CHANGED' if 'ATTEMPT_CHANGED' in str(exc) else 'JOB_UNAVAILABLE')
                add(finding(code, 'parser' if isinstance(exc, ImportError) else 'binding', scope='job',
                    severity='ERROR', evidence={'error_type': type(exc).__name__},
                    caused_by='DEPENDENCY_MISSING' if isinstance(exc, ImportError) else None))
                report['observation']['status'] = 'UNAVAILABLE'
    return report


def format_report(report):
    """Compact frozen text; no repair command is executed or implied by this view."""
    from cmw.jobs.snapshot import safe_snapshot_text
    lines = ['CMW Doctor — read only', f"Scope: {report.get('scope', 'unknown')}"]
    caller = report.get('caller_runtime') or report.get('runtime') or {}
    for label, runtime in (('Caller', caller), ('Endpoint', report.get('endpoint_runtime'))):
        if not runtime:
            continue
        identity = runtime.get('identity') or {}
        lines.extend([f"{label}: {runtime.get('distribution', {}).get('version', 'unknown')}",
                      f"  Python: {runtime.get('origins', {}).get('executable', 'unknown')}",
                      f"  Package: {runtime.get('origins', {}).get('package', 'unknown')}",
                      f"  Fingerprint: {identity.get('fingerprint', 'unknown')}"])
    if report.get('target'):
        lines.append('Target: ' + json.dumps(report['target'], sort_keys=True))
    lines.append('Worker runtime: ' + report.get('worker', {}).get('status', 'UNKNOWN'))
    for item in report.get('findings', []):
        lines.extend([f"[{item['severity']}] {item['domain']}: {item['code']}",
                      '  ' + item['summary'], '  Next read-only check: ' + item['next_check']])
    lines.extend(['Scientific policy: NOT ASSESSED', report.get('qualification', QUALIFICATION)])
    return safe_snapshot_text('\n'.join(lines))
