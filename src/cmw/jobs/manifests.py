"""Native job manifests, Phase 1a (issue #41): single-node, owner-only.

The project owner holds ``.cmw-project.json``, the ID ledger and the integrity
journal on its local filesystem. ``reserve`` allocates ``J<n>`` and creates
``<project>/jobs/J<n>/``; ``add_native`` freezes a read-only ``job.json``,
anchors its exact SHA-256 in the journal, then registers the job held in the
existing queue. The queue database stays the operational authority; these files
add portable identity and provenance only. Non-owner nodes refuse; there is no
fallback allocator. Remote allocation (1b), state.json (2), annotations and
checking (3) and backfill (4) are later phases. See docs/design/job-manifests.md.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from uuid import UUID, uuid4

from .store import JobsError

SCHEMA_VERSION = 1
PROJECT_FILE = '.cmw-project.json'
LEDGER = Path('.cmw') / 'ids.sqlite3'
JOURNAL = Path('.cmw') / 'integrity.jsonl'
JOURNAL_LOCK = Path('.cmw') / 'integrity.lock'
MANIFEST_DIR = '.cmw-manifest'
NATIVE_ID = re.compile(r'J([1-9]\d*)')
LEGACY_NAME = re.compile(r'J(\d+)(?:\.\d+)?')
KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')
NODE_UUID = re.compile(r'[0-9a-f]{32}')
SHA256 = re.compile(r'[0-9a-f]{64}')
ENVIRONMENT_PREFIXES = ('OMP_', 'OPENBLAS_', 'MKL_', 'VECLIB_', 'GFORTRAN_', 'OMPI_MCA_')
VASP_INPUTS = ('INCAR', 'POSCAR', 'KPOINTS')
JOURNAL_EVENTS = {'manifest', 'registration'}
LEDGER_TIMEOUT_SECONDS = 5.0


class ManifestError(JobsError):
    pass


def _utc_now():
    now = time.time()
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(now)) + f'.{int(now % 1 * 1e6):06d}Z'


def canonical(value):
    """The exact published bytes; their SHA-256 is the integrity anchor."""
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + '\n').encode()


def request_uuid(value=None):
    if value is None:
        return uuid4().hex
    try:
        parsed = UUID(str(value))
    except ValueError as exc:
        raise ManifestError('Request UUID must be a UUID') from exc
    return parsed.hex


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_new(path, raw, mode):
    """No-clobber publication of complete bytes; never replaces an existing file."""
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        try:
            os.link(name, path)
        except FileExistsError as exc:
            raise ManifestError(f'Refusing to overwrite existing {path}') from exc
        _fsync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def _root(path):
    root = Path(path).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ManifestError(f'Project root is not a directory: {root}')
    return root


def _local_node(store):
    """Native node fields use the enrolled queue's node UUID, never an alias."""
    identity = store.queue_identity()
    node_id = (identity or {}).get('node_id')
    if not isinstance(node_id, str) or not NODE_UUID.fullmatch(node_id):
        raise ManifestError('Native manifests require a queue enrolled to a node UUID (cmw nodes enroll)')
    return identity


# Project identity and bootstrap -------------------------------------------------

def legacy_high_water(inventory, project_key):
    """Highest integer component of every reviewed legacy queue ID and label.

    The inventory is the reviewed all-node, all-queue listing (including retained
    history). Names with digits outside ``J<n>[.<m>]`` must be listed as reviewed
    exceptions; nothing is silently omitted.
    """
    if not isinstance(inventory, dict) or set(inventory) != {
            'schema_version', 'project_key', 'complete', 'names', 'reviewed_other_names'}:
        raise ManifestError('Legacy inventory must have exactly schema_version, project_key, complete, '
                            'names and reviewed_other_names')
    if inventory['schema_version'] != 1 or inventory['project_key'] != project_key:
        raise ManifestError('Legacy inventory schema or project key does not match')
    if inventory['complete'] is not True:
        raise ManifestError('Legacy inventory is not marked complete; incomplete history blocks bootstrap')
    names, others = inventory['names'], inventory['reviewed_other_names']
    if (not isinstance(names, list) or not isinstance(others, list)
            or not all(isinstance(item, str) and item for item in names + others)):
        raise ManifestError('Legacy inventory names must be lists of nonempty strings')
    highest = 0
    for name in names:
        match = LEGACY_NAME.fullmatch(name)
        if match:
            highest = max(highest, int(match[1]))
        elif re.search(r'\d', name) and name not in others:
            raise ManifestError(f'Legacy name {name!r} has digits outside J<n>[.<m>]; '
                                'review it and list it in reviewed_other_names')
    return highest


def init_project(store, root, project_key, *, inventory=None):
    """Create the owner's project file and ledger. Refuses to touch existing ones."""
    root = _root(root)
    if not isinstance(project_key, str) or len(project_key) > 128 or not KEY.fullmatch(project_key):
        raise ManifestError('Project key must match [A-Za-z0-9][A-Za-z0-9._-]* (at most 128)')
    owner = _local_node(store)['node_id']
    high_water = 0 if inventory is None else legacy_high_water(inventory, project_key)
    project_path, state = root / PROJECT_FILE, root / '.cmw'
    if project_path.exists() or project_path.is_symlink() or state.exists() or state.is_symlink():
        raise ManifestError(f'Project is already initialised (or partially): {root}')
    state.mkdir(mode=0o700)
    inventory_digest = None if inventory is None else hashlib.sha256(canonical(inventory)).hexdigest()
    with _ledger(root, create=True) as con:
        con.execute('BEGIN IMMEDIATE')
        con.executemany('INSERT INTO meta(key, value) VALUES (?, ?)', [
            ('schema_version', '1'), ('project_key', project_key), ('owner_node_id', owner),
            ('legacy_high_water', str(high_water)), ('next', str(high_water + 1)),
            ('legacy_inventory_sha256', inventory_digest or ''),
        ])
        con.execute('COMMIT')
    project = {'schema_version': SCHEMA_VERSION, 'project_key': project_key, 'owner_node_id': owner,
               'legacy_nodes': {}, 'legacy_queues': {}}
    # The project file is published last: its presence enables native creation.
    _publish_new(project_path, canonical(project), 0o644)
    return {'project': project, 'root': str(root), 'legacy_high_water': high_water,
            'first_id': f'J{high_water + 1}', 'legacy_inventory_sha256': inventory_digest}


def load_project(root):
    root = _root(root)
    try:
        project = json.loads((root / PROJECT_FILE).read_text(encoding='utf-8'))
    except FileNotFoundError as exc:
        raise ManifestError(f'Not a manifest-enabled project (no {PROJECT_FILE}): {root}') from exc
    except (OSError, ValueError) as exc:
        raise ManifestError(f'Unreadable {PROJECT_FILE}: {exc}') from exc
    if (not isinstance(project, dict)
            or set(project) != {'schema_version', 'project_key', 'owner_node_id', 'legacy_nodes', 'legacy_queues'}
            or project['schema_version'] != SCHEMA_VERSION
            or not isinstance(project['project_key'], str) or not KEY.fullmatch(project['project_key'])
            or not isinstance(project['owner_node_id'], str) or not NODE_UUID.fullmatch(project['owner_node_id'])
            or not isinstance(project['legacy_nodes'], dict) or not isinstance(project['legacy_queues'], dict)):
        raise ManifestError(f'{PROJECT_FILE} does not match the v1 project contract')
    return root, project


def _require_owner(store, project):
    identity = _local_node(store)
    if identity['node_id'] != project['owner_node_id']:
        raise ManifestError('This node does not own the project ID ledger; native job creation is refused '
                            '(remote allocation is not available yet)')
    return identity


def enclosing_project(directory):
    """The manifest-enabled project whose jobs/ tree contains directory, if any."""
    directory = Path(directory)
    for parent in directory.parents:
        if parent.name == 'jobs' and (parent.parent / PROJECT_FILE).exists():
            return parent.parent
    return None


# ID ledger ---------------------------------------------------------------------

@contextmanager
def _ledger(root, *, create=False):
    path = root / LEDGER
    if not create and not path.is_file():
        raise ManifestError(f'Project ID ledger is missing: {path}')
    con = sqlite3.connect(path, timeout=LEDGER_TIMEOUT_SECONDS, isolation_level=None)
    try:
        if create:
            os.chmod(path, 0o600)
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            con.execute('CREATE TABLE reservations(number INTEGER PRIMARY KEY, request_uuid TEXT NOT NULL UNIQUE, '
                        'intent_sha256 TEXT NOT NULL, reserved_at TEXT NOT NULL)')
        con.execute('PRAGMA synchronous=FULL')
        yield con
    except sqlite3.OperationalError as exc:
        if 'locked' in str(exc) or 'busy' in str(exc):
            raise ManifestError('Project ID ledger is busy; retry the same request UUID') from exc
        raise ManifestError(f'Project ID ledger unavailable: {exc}') from exc
    finally:
        con.close()


def _meta(con):
    return dict(con.execute('SELECT key, value FROM meta'))


def _allocate(root, project, request, intent):
    """One BEGIN IMMEDIATE transaction: counter, permanent reservation, request outcome."""
    digest = hashlib.sha256(canonical(intent)).hexdigest()
    with _ledger(root) as con:
        con.execute('BEGIN IMMEDIATE')
        try:
            meta = _meta(con)
            if (meta.get('project_key'), meta.get('owner_node_id')) != (project['project_key'], project['owner_node_id']):
                raise ManifestError('Project ID ledger belongs to a different project or owner')
            row = con.execute('SELECT number, intent_sha256 FROM reservations WHERE request_uuid=?',
                              (request,)).fetchone()
            if row is not None:
                if row[1] != digest:
                    raise ManifestError('Request UUID already reserved an ID for a different intent')
                con.execute('ROLLBACK')
                return row[0], False
            number = int(meta['next'])
            highest = con.execute('SELECT MAX(number) FROM reservations').fetchone()[0] or 0
            if number <= max(highest, int(meta['legacy_high_water'])):
                raise ManifestError('Project ID ledger counter is behind its reservations; refusing to reuse an ID')
            con.execute('INSERT INTO reservations VALUES (?, ?, ?, ?)', (number, request, digest, _utc_now()))
            con.execute("UPDATE meta SET value=? WHERE key='next'", (str(number + 1),))
            con.execute('COMMIT')
            return number, True
        except BaseException:
            if con.in_transaction:
                con.execute('ROLLBACK')
            raise


def _reserved(root, number):
    with _ledger(root) as con:
        return con.execute('SELECT 1 FROM reservations WHERE number=?', (number,)).fetchone() is not None


def reserve(store, root, *, request=None):
    """Allocate the next native ID and create its job directory. IDs are never reused."""
    root, project = load_project(root)
    _require_owner(store, project)
    request = request_uuid(request)
    number, new = _allocate(root, project, request, {'operation': 'reserve', 'project': project['project_key']})
    job_id = f'J{number}'
    jobs = root / 'jobs'
    jobs.mkdir(exist_ok=True)
    directory = jobs / job_id
    try:
        directory.mkdir()
    except FileExistsError:
        if directory.is_symlink() or not directory.is_dir():
            raise ManifestError(f'Reserved job path is not a plain directory: {directory}')
    return {'project': project['project_key'], 'id': job_id, 'job_directory': str(directory),
            'request_uuid': request, 'new_reservation': new}


# Integrity journal -------------------------------------------------------------

def read_journal(root):
    """Complete, contiguous records; an incomplete or corrupt tail blocks writes."""
    path = root / JOURNAL
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    records = []
    lines = data.split(b'\n')
    if lines[-1] != b'':
        raise ManifestError(f'Integrity journal has an incomplete tail after record {len(lines) - 1}; '
                            'explicit recovery review is required')
    for index, line in enumerate(lines[:-1], 1):
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise ManifestError(f'Integrity journal record {index} is not valid JSON') from exc
        if not isinstance(record, dict) or record.get('sequence') != index:
            raise ManifestError(f'Integrity journal sequence is broken at record {index}')
        records.append(record)
    return records


@contextmanager
def _journal_lock(root):
    fd = os.open(root / JOURNAL_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def append_journal(root, project_key, event, request, payload):
    """Append one receipt; identical (event, request) retries return the original."""
    if event not in JOURNAL_EVENTS:
        raise ManifestError(f'Unsupported integrity event: {event}')
    with _journal_lock(root):
        records = read_journal(root)
        for record in records:
            if (record['event'], record['request_uuid']) == (event, request):
                if record['payload'] != payload or record['project'] != project_key:
                    raise ManifestError(f'Request UUID already anchored a different {event} receipt')
                return record
        record = {'schema_version': SCHEMA_VERSION, 'sequence': len(records) + 1, 'request_uuid': request,
                  'event': event, 'timestamp': _utc_now(), 'project': project_key, 'payload': payload}
        path = root / JOURNAL
        created = not path.exists()
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, json.dumps(record, sort_keys=True, ensure_ascii=True, allow_nan=False).encode() + b'\n')
            os.fsync(fd)
        finally:
            os.close(fd)
        if created:
            _fsync_directory(path.parent)
        return record


# job.json ----------------------------------------------------------------------

def _runtime_fingerprint():
    try:
        from cmw.nodes.identity import runtime_identity
        value = runtime_identity().get('fingerprint')
    except Exception:  # Optional evidence: unknown stays null, never invented.
        return None
    return value if isinstance(value, str) and SHA256.fullmatch(value) else None


def _vasp_evidence(directory, argv):
    """Synchronous input hashes and POTCAR dataset identities from jobs/J<n>/inputs."""
    from cmw.core.provenance import file_hash
    from cmw.periodic.vasp.potentials import check_potcar
    from cmw.periodic.vasp.result_runner import RESTART_FILES
    from .location_resolvers import vasp_runner_arguments
    try:
        route = vasp_runner_arguments({'argv': argv, 'cwd': str(directory)})
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestError(f'Unresolved run-vasp.sh arguments: {exc}') from exc
    if route is None or '--managed-foreground' not in route[0] or '--dry-run' in route[0]:
        raise ManifestError('Native VASP jobs require the managed run-vasp.sh route (--managed-foreground)')
    options, source, output = route
    if (source, output) != (directory / 'inputs', directory / 'run'):
        raise ManifestError(f'Native VASP jobs use --input {directory / "inputs"} and --output {directory / "run"}')
    if '--restart-from' in options:
        raise ManifestError('--restart-from is not supported yet')
    try:
        inputs = {name: file_hash(source / name) for name in VASP_INPUTS}
        datasets = check_potcar(source / 'POSCAR', source / 'POTCAR')
        restart = {name: file_hash(source / name)
                   for name in RESTART_FILES.get(options.get('--restart', 'none'), ())}
    except (OSError, ValueError) as exc:
        raise ManifestError(f'Native VASP inputs are not complete and valid: {exc}') from exc
    potcar = [{'element': item.element, 'label': item.label, 'family': item.family,
               'dataset_date': item.date, 'sha256': item.sha256, 'block_index': index}
              for index, item in enumerate(datasets)]
    return inputs, potcar, restart


def build_job_manifest(*, project, job_id, node_id, engine, directory, argv, mpi_ranks, environment):
    if engine == 'VASP':
        inputs, potcar, restart_inputs = _vasp_evidence(directory, argv)
    else:
        inputs, potcar, restart_inputs = dict.fromkeys(VASP_INPUTS), None, {}
    selected = {key: value for key, value in environment.items() if key.startswith(ENVIRONMENT_PREFIXES)}
    return {
        'schema_version': SCHEMA_VERSION, 'id': job_id, 'project': project['project_key'],
        'provenance': 'native', 'created_at': _utc_now(),
        'authoring_node': node_id, 'target_node': node_id, 'id_owner_node': project['owner_node_id'],
        'engine': engine, 'inputs': inputs, 'potcar': potcar, 'preparation_record': None,
        # No verified VASP build evidence is advertised yet (D22); never substitute the runner.
        'executable': None, 'runtime_fingerprint': _runtime_fingerprint(),
        'launch': {'argv': list(argv), 'mpi_ranks': mpi_ranks, 'environment': selected},
        'parent': None, 'parent_inferred': False, 'restart_inputs': restart_inputs,
    }


def _native_target(root, cwd):
    directory = Path(cwd).expanduser().resolve(strict=True)
    match = NATIVE_ID.fullmatch(directory.name)
    if directory.parent != root / 'jobs' or match is None:
        raise ManifestError(f'Native jobs run in {root / "jobs"}/J<n>; reserve one first')
    if not _reserved(root, int(match[1])):
        raise ManifestError(f'{directory.name} was not reserved in this project ledger')
    return directory, directory.name


def add_native(store, root, *, cwd, argv, engine, request=None, **add_options):
    """Freeze job.json, anchor it, then register the job on Hold in the local queue.

    A published-but-unregistered ID is burned, never republished or reused.
    """
    from .store import normalized_argv
    root, project = load_project(root)
    identity = _require_owner(store, project)
    if engine not in {'VASP', 'ORCA', 'Multiwfn', 'Command'}:
        raise ManifestError('Native manifests support engines VASP, ORCA, Multiwfn and Command')
    directory, job_id = _native_target(root, cwd)
    request = request_uuid(request)
    path = directory / MANIFEST_DIR / 'job.json'
    if path.exists() or path.is_symlink():
        registered = [record for record in read_journal(root)
                      if record['event'] == 'registration' and record['payload']['id'] == job_id]
        if registered:
            raise ManifestError(f'{job_id} is already registered as queue job '
                                f'{registered[0]["payload"]["binding"]["job_id"]}')
        raise ManifestError(f'{job_id} already has a published job.json without a registration; '
                            'the ID is burned, reserve a new one')
    manifest = build_job_manifest(project=project, job_id=job_id, node_id=identity['node_id'], engine=engine,
                                  directory=directory, argv=normalized_argv(argv), mpi_ranks=add_options.get('mpi_ranks'),
                                  environment=dict(add_options.get('env') or {}))
    raw = canonical(manifest)
    digest = hashlib.sha256(raw).hexdigest()
    (directory / MANIFEST_DIR).mkdir(exist_ok=True)
    _publish_new(path, raw, 0o444)
    key = project['project_key']
    append_journal(root, key, 'manifest', request, {'id': job_id, 'job_sha256': digest, 'binding': None})
    # Registration stays held: release and dispatch keep their explicit operations.
    job = store.add(argv=argv, cwd=directory, engine=engine, hold=True,
                    manifest={'project': key, 'id': job_id, 'root': str(root), 'job_sha256': digest},
                    **add_options)
    binding = {'node_id': identity['node_id'], 'queue_id': identity['queue_id'],
               'job_id': job['id'], 'attempt_id': job['attempt_id']}
    append_journal(root, key, 'registration', request, {'id': job_id, 'job_sha256': digest, 'binding': binding})
    return job
