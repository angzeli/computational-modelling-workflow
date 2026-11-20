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
import stat
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
JOURNAL_EVENTS = {'manifest', 'registration', 'queue_binding', 'legacy_alias'}
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


def init_project(store, root, project_key, *, inventory=None, no_legacy=False):
    """Create the owner's project file and ledger. Refuses to touch existing ones.

    Exactly one of a reviewed inventory or the explicit no-legacy assertion is
    required; an absent or null inventory never stands in for the assertion.
    """
    if no_legacy == (inventory is not None) or no_legacy not in (True, False):
        raise ManifestError('Provide exactly one of a legacy inventory or the no-legacy-IDs assertion')
    root = _root(root)
    if not isinstance(project_key, str) or len(project_key) > 128 or not KEY.fullmatch(project_key):
        raise ManifestError('Project key must match [A-Za-z0-9][A-Za-z0-9._-]* (at most 128)')
    owner = _local_node(store)['node_id']
    high_water = 0 if no_legacy else legacy_high_water(inventory, project_key)
    project_path, state = root / PROJECT_FILE, root / '.cmw'
    if project_path.exists() or project_path.is_symlink() or state.exists() or state.is_symlink():
        raise ManifestError(f'Project is already initialised (or partially): {root}')
    state.mkdir(mode=0o700)
    inventory_digest = None if no_legacy else hashlib.sha256(canonical(inventory)).hexdigest()
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

def journal_prefix(root):
    """The complete, contiguous records and the first damage found after them (or None)."""
    path = root / JOURNAL
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return [], None
    records = []
    lines = data.split(b'\n')
    for index, line in enumerate(lines[:-1], 1):
        try:
            record = json.loads(line)
        except ValueError:
            return records, f'Integrity journal record {index} is not valid JSON'
        if not isinstance(record, dict) or record.get('sequence') != index:
            return records, f'Integrity journal sequence is broken at record {index}'
        records.append(record)
    if lines[-1] != b'':
        return records, (f'Integrity journal has an incomplete tail after record {len(lines) - 1}; '
                         'explicit recovery review is required')
    return records, None


def read_journal(root):
    """Complete, contiguous records; an incomplete or corrupt tail blocks writes."""
    records, damage = journal_prefix(root)
    if damage is not None:
        raise ManifestError(damage)
    return records


@contextmanager
def _journal_lock(root):
    fd = os.open(root / JOURNAL_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def append_journal(root, project_key, event, request, payload, admit=None):
    """Append one receipt; identical (event, request) retries return the original.

    ``admit(records)`` may refuse a new receipt (by raising) under the same lock.
    """
    if event not in JOURNAL_EVENTS:
        raise ManifestError(f'Unsupported integrity event: {event}')
    with _journal_lock(root):
        records = read_journal(root)
        for record in records:
            if (record['event'], record['request_uuid']) == (event, request):
                if record['payload'] != payload or record['project'] != project_key:
                    raise ManifestError(f'Request UUID already anchored a different {event} receipt')
                return record
        if admit is not None:
            admit(records)
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


def _vasp_route(directory, argv):
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
        raise ManifestError('The runner\'s own --restart-from is unsupported; use cmw jobs add --restart-from')
    return options, source


def _vasp_evidence(directory, argv, copied=None):
    """Synchronous input hashes and POTCAR dataset identities from jobs/J<n>/inputs.

    ``copied`` holds digests already verified while copying from a parent, so
    multi-GiB checkpoints are not read again.
    """
    from cmw.core.provenance import file_hash
    from cmw.periodic.vasp.potentials import check_potcar
    from cmw.periodic.vasp.result_runner import RESTART_FILES
    options, source = _vasp_route(directory, argv)
    try:
        inputs = {name: file_hash(source / name) for name in VASP_INPUTS}
        datasets = check_potcar(source / 'POSCAR', source / 'POTCAR')
        restart = {name: (copied or {}).get(name) or file_hash(source / name)
                   for name in RESTART_FILES.get(options.get('--restart', 'none'), ())}
    except (OSError, ValueError) as exc:
        raise ManifestError(f'Native VASP inputs are not complete and valid: {exc}') from exc
    potcar = [{'element': item.element, 'label': item.label, 'family': item.family,
               'dataset_date': item.date, 'sha256': item.sha256, 'block_index': index}
              for index, item in enumerate(datasets)]
    return inputs, potcar, restart


# Restart lineage -----------------------------------------------------------------

def _parent_run(store, root, reference):
    """The terminated, receipt-verified native VASP parent's run directory."""
    from .store import TERMINAL
    if not isinstance(reference, str) or not NATIVE_ID.fullmatch(reference):
        raise ManifestError('--restart-from takes a native J<n> in this project; legacy and cross-project '
                            'parents are not supported yet')
    registrations = [record['payload'] for record in read_journal(root)
                     if record['event'] == 'registration' and record['payload']['id'] == reference]
    if len(registrations) != 1:
        raise ManifestError(f'Parent {reference} has no single registration receipt in this project')
    receipt = registrations[0]
    parent = root / 'jobs' / reference
    try:
        raw = (parent / MANIFEST_DIR / 'job.json').read_bytes()
    except OSError as exc:
        raise ManifestError(f'Parent {reference} job.json is unreadable: {exc}') from exc
    if hashlib.sha256(raw).hexdigest() != receipt['job_sha256']:
        raise ManifestError(f'Parent {reference} job.json differs from its integrity receipt')
    if json.loads(raw).get('engine') != 'VASP':
        raise ManifestError(f'Parent {reference} is not a VASP job')
    binding = receipt['binding']
    if binding['queue_id'] != (store.queue_identity() or {}).get('queue_id'):
        raise ManifestError(f'Parent {reference} ran in another queue; restart-file transport is out of scope')
    with store.read_transaction() as con:
        job = store.get(con, binding['job_id'])
    if job['attempt_id'] != binding['attempt_id']:
        raise ManifestError(f'Parent {reference} queue attempt differs from its registration receipt')
    if job['status'] not in TERMINAL:
        raise ManifestError(f'Parent {reference} is {job["status"]}; checkpoints are copied only after it terminates')
    return parent / 'run'


def _copy_checkpoint(source, destination):
    """Copy-only staging: hash the source while copying, verify the copy, require an unchanged source."""
    from cmw.core.provenance import file_hash
    try:
        before = source.lstat()
    except FileNotFoundError as exc:
        raise ManifestError(f'Parent checkpoint {source} does not exist') from exc
    if not stat.S_ISREG(before.st_mode) or before.st_size == 0:
        raise ManifestError(f'Parent checkpoint {source} must be a nonempty regular file')
    digest = hashlib.sha256()
    fd, name = tempfile.mkstemp(prefix=f'.{destination.name}.', dir=destination.parent)
    try:
        with source.open('rb') as reader, os.fdopen(fd, 'wb') as writer:
            for chunk in iter(lambda: reader.read(8 * 1024 * 1024), b''):
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fchmod(writer.fileno(), 0o444)
            os.fsync(writer.fileno())
        try:
            os.link(name, destination)
        except FileExistsError as exc:
            raise ManifestError(f'Refusing to overwrite existing {destination}') from exc
    finally:
        Path(name).unlink(missing_ok=True)
    try:
        after = source.lstat()
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')
        if any(getattr(before, key) != getattr(after, key) for key in fields):
            raise ManifestError(f'Parent checkpoint {source} changed while it was copied')
        if file_hash(destination) != digest.hexdigest():
            raise ManifestError(f'Copied checkpoint {destination} differs from its source')
    except BaseException:
        destination.unlink(missing_ok=True)  # Only the copy this call published.
        raise
    return digest.hexdigest()


def _stage_parent_checkpoints(store, root, directory, argv, reference):
    from cmw.periodic.vasp.result_runner import RESTART_FILES
    options, inputs = _vasp_route(directory, argv)
    names = RESTART_FILES.get(options.get('--restart', 'none'), ())
    if not names:
        raise ManifestError('--restart-from needs the runner\'s --restart wavecar|chgcar|both; '
                            'INCAR restart settings remain yours to set')
    run = _parent_run(store, root, reference)
    for name in names:
        if os.path.lexists(inputs / name):
            raise ManifestError(f'{inputs / name} already exists; --restart-from never reuses an existing checkpoint')
    copied = {}
    try:
        for name in names:
            copied[name] = _copy_checkpoint(run / name, inputs / name)
    except BaseException:
        _remove_copies(inputs, copied)
        raise
    return copied


def _remove_copies(inputs, copied):
    for name in copied:
        (inputs / name).unlink(missing_ok=True)


def build_job_manifest(*, project, job_id, node_id, engine, directory, argv, mpi_ranks, environment,
                       parent=None, copied=None):
    if engine == 'VASP':
        inputs, potcar, restart_inputs = _vasp_evidence(directory, argv, copied)
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
        'parent': parent, 'parent_inferred': False, 'restart_inputs': restart_inputs,
    }


def _native_target(root, cwd):
    directory = Path(cwd).expanduser().resolve(strict=True)
    match = NATIVE_ID.fullmatch(directory.name)
    if directory.parent != root / 'jobs' or match is None:
        raise ManifestError(f'Native jobs run in {root / "jobs"}/J<n>; reserve one first')
    if not _reserved(root, int(match[1])):
        raise ManifestError(f'{directory.name} was not reserved in this project ledger')
    return directory, directory.name


def add_native(store, root, *, cwd, argv, engine, request=None, restart_from=None, **add_options):
    """Freeze job.json, anchor it, then register the job on Hold in the local queue.

    ``restart_from`` copies the terminated parent's checkpoints for the runner's
    ``--restart`` mode into ``inputs/`` and records the parent and copy digests.
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
    sidecar = directory / MANIFEST_DIR
    try:
        sidecar.mkdir()
    except FileExistsError:
        pass
    # mkdir(exist_ok) and publication follow symlinks; the sidecar must be this job's own directory.
    if not stat.S_ISDIR(sidecar.lstat().st_mode) or sidecar.resolve(strict=True) != sidecar:
        raise ManifestError(f'{sidecar} must be a real directory inside the reserved job')
    copied = None
    if restart_from is not None:
        if engine != 'VASP':
            raise ManifestError('--restart-from currently supports VASP checkpoints only')
        copied = _stage_parent_checkpoints(store, root, directory, argv, restart_from)
    try:
        manifest = build_job_manifest(project=project, job_id=job_id, node_id=identity['node_id'], engine=engine,
                                      directory=directory, argv=normalized_argv(argv),
                                      mpi_ranks=add_options.get('mpi_ranks'),
                                      environment=dict(add_options.get('env') or {}),
                                      parent=restart_from, copied=copied)
        raw = canonical(manifest)
        digest = hashlib.sha256(raw).hexdigest()
        _publish_new(path, raw, 0o444)
    except BaseException:
        # Until job.json exists the reserved job must look untouched, so a retry can copy again.
        if copied and not os.path.lexists(path):
            _remove_copies(directory / 'inputs', copied)
        raise
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


# Remote targets (Phase 1b) -----------------------------------------------------
#
# The owner is the frontend: it allocates the ID, freezes job.json from the
# target's own submission preview, anchors it, and records the target's
# registration. The target never allocates; it verifies the exact bytes against
# its plan and publishes them beside the job under
# <storage root>/<project key>/jobs/J<n>/. Restart files never travel.

REMOTE_ENGINES = {'VASP', 'Command'}
NATIVE_CAPABILITY = {'schema': 1, 'engines': sorted(REMOTE_ENGINES), 'layout': 'storage/project/jobs/ID'}


def validate_native(native):
    """The portable spec's native block: the owner's project record and the reserved ID."""
    if not isinstance(native, dict) or set(native) != {'project', 'id'}:
        raise ManifestError('Native submission needs exactly the project record and the reserved ID')
    project = native['project']
    if (not isinstance(project, dict)
            or set(project) != {'schema_version', 'project_key', 'owner_node_id', 'legacy_nodes', 'legacy_queues'}
            or project['schema_version'] != SCHEMA_VERSION
            or not isinstance(project['project_key'], str) or not KEY.fullmatch(project['project_key'])
            or not isinstance(project['owner_node_id'], str) or not NODE_UUID.fullmatch(project['owner_node_id'])
            or project['legacy_nodes'] != {} or project['legacy_queues'] != {}):
        raise ManifestError('Native submission project record does not match the v1 contract')
    if not isinstance(native['id'], str) or not NATIVE_ID.fullmatch(native['id']):
        raise ManifestError('Native submission ID must be J<n>')
    return native


def remote_fields(*, native, spec, plan, potcar, input_digests):
    """Every job.json field both sides derive identically from the submission."""
    if spec['engine'] not in REMOTE_ENGINES:
        raise ManifestError('Native remote submission supports VASP and Command')
    runtime = plan.get('runtime_id')
    return {
        'schema_version': SCHEMA_VERSION, 'id': native['id'], 'project': native['project']['project_key'],
        'provenance': 'native', 'target_node': spec['node_id'],
        'id_owner_node': native['project']['owner_node_id'], 'engine': spec['engine'],
        'inputs': ({name: input_digests[f'inputs/{name}'] for name in VASP_INPUTS} if spec['engine'] == 'VASP'
                   else dict.fromkeys(VASP_INPUTS)),
        'potcar': potcar if spec['engine'] == 'VASP' else None, 'preparation_record': None,
        'executable': None,
        'runtime_fingerprint': runtime if isinstance(runtime, str) and SHA256.fullmatch(runtime) else None,
        'launch': {'argv': list(plan['argv']), 'mpi_ranks': spec['resources']['mpi_ranks'], 'environment': {}},
        'parent': None, 'parent_inferred': False, 'restart_inputs': {},
    }


def potcar_records(poscar, potcar):
    from cmw.periodic.vasp.potentials import check_potcar
    try:
        datasets = check_potcar(poscar, potcar)
    except (OSError, ValueError) as exc:
        raise ManifestError(f'POTCAR does not match POSCAR: {exc}') from exc
    return [{'element': item.element, 'label': item.label, 'family': item.family,
             'dataset_date': item.date, 'sha256': item.sha256, 'block_index': index}
            for index, item in enumerate(datasets)]


def begin_remote(store, root, request):
    """Owner check and idempotent reservation keyed by the submission request ID."""
    root, project = load_project(root)
    identity = _require_owner(store, project)
    reserved = reserve(store, root, request=request)
    return {'root': root, 'project': project, 'node_id': identity['node_id'], 'id': reserved['id'],
            'native': {'project': project, 'id': reserved['id']}}


def _spool_file(spool, manifest, path):
    record = json.loads((spool / 'spool.json').read_text(encoding='utf-8'))
    for item, source in zip(record['manifest']['files'], record['sources']):
        if item['path'] == path:
            return spool / source['flat']
    raise ManifestError(f'Submission bundle has no {path}')


def anchor_remote(owner, *, request, spec, plan, spool):
    """Freeze job.json on the owner (reused verbatim on retry) and anchor it."""
    root, job_id = owner['root'], owner['id']
    path = root / 'jobs' / job_id / MANIFEST_DIR / 'job.json'
    if os.path.lexists(path):  # A retry reuses the exact anchored bytes.
        raw = anchored_job(owner, request)
        return json.loads(raw), raw
    files = {item['path']: item['sha256'] for item in spec['manifest']['files']}
    potcar = None
    if spec['engine'] == 'VASP':
        potcar = potcar_records(_spool_file(spool, spec['manifest'], 'inputs/POSCAR'),
                                _spool_file(spool, spec['manifest'], 'inputs/POTCAR'))
    job = {**remote_fields(native=owner['native'], spec=spec, plan=plan, potcar=potcar, input_digests=files),
           'created_at': _utc_now(), 'authoring_node': owner['node_id']}
    raw = canonical(job)
    (path.parent).mkdir(exist_ok=True)
    _publish_new(path, raw, 0o444)
    append_journal(root, owner['project']['project_key'], 'manifest', request,
                   {'id': job_id, 'job_sha256': hashlib.sha256(raw).hexdigest(), 'binding': None})
    return job, raw


def anchored_job(owner, request):
    """The owner's frozen job.json for this request, verified against its manifest receipt."""
    path = owner['root'] / 'jobs' / owner['id'] / MANIFEST_DIR / 'job.json'
    anchored = [record for record in read_journal(owner['root'])
                if record['event'] == 'manifest' and record['request_uuid'] == request]
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ManifestError(f'{owner["id"]} job.json is missing on the owner: {exc}') from exc
    if not anchored or anchored[0]['payload']['job_sha256'] != hashlib.sha256(raw).hexdigest():
        raise ManifestError(f'{owner["id"]} job.json does not match its manifest receipt')
    return raw


def record_remote_registration(owner, *, request, raw, job, spec):
    """Bind the ID to the target's queue job; replays return the original receipt."""
    digest = hashlib.sha256(raw).hexdigest()
    recorded = job.get('manifest') or {}
    if recorded.get('id') != owner['id'] or recorded.get('job_sha256') != digest:
        raise ManifestError('Target registration does not carry this job.json; no registration receipt was written')
    binding = {'node_id': spec['node_id'], 'queue_id': spec['queue_id'],
               'job_id': job['id'], 'attempt_id': job['attempt_id']}
    return append_journal(owner['root'], owner['project']['project_key'], 'registration', request,
                          {'id': owner['id'], 'job_sha256': digest, 'binding': binding})


def target_layout(storage_root, native):
    project = Path(storage_root) / native['project']['project_key']
    return project, project / 'jobs' / native['id']


def accept_remote(*, config, spec, plan, bundle, job):
    """Target side: verify the owner's job.json against this node's own plan."""
    native = validate_native(spec['native'])
    if not isinstance(job, dict):
        raise ManifestError('Native submission requires the job.json object')
    raw = canonical(job)
    expected = remote_fields(
        native=native, spec=spec, plan=plan, input_digests={item['path']: item['sha256'] for item in spec['manifest']['files']},
        potcar=potcar_records(bundle / 'inputs' / 'POSCAR', bundle / 'inputs' / 'POTCAR') if spec['engine'] == 'VASP' else None)
    if expected['target_node'] != config['node_id']:
        raise ManifestError('job.json targets another node')
    chosen = {key: job.get(key) for key in ('created_at', 'authoring_node')}
    if ({key: value for key, value in job.items() if key not in chosen} != expected
            or not isinstance(chosen['authoring_node'], str) or not NODE_UUID.fullmatch(chosen['authoring_node'])
            or not isinstance(chosen['created_at'], str)
            or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z', chosen['created_at'])):
        raise ManifestError('job.json differs from this node\'s plan; the target refuses drift rather than rewriting it')
    return raw


def publish_remote(*, project_dir, work, native, raw, request_id, queue_id):
    """Project replica (owner record, no ledger) and the job's read-only job.json."""
    replica = canonical(native['project'])
    project_file = project_dir / PROJECT_FILE
    if os.path.lexists(project_file):
        if project_file.read_bytes() != replica:
            raise ManifestError(f'{project_file} records a different project or owner')
    else:
        # Only a directory this registration just created may become a replica.
        unexpected = {item.name for item in project_dir.iterdir()} - {'jobs'}
        unexpected |= {item.name for item in (project_dir / 'jobs').iterdir()} - {work.name}
        if unexpected or any(work.iterdir()):
            raise ManifestError(f'{project_dir} already holds other files; it cannot become a project replica')
        _publish_new(project_file, replica, 0o644)
    sidecar = work / MANIFEST_DIR
    try:
        sidecar.mkdir(mode=0o700)
    except FileExistsError:
        pass
    if not stat.S_ISDIR(sidecar.lstat().st_mode) or sidecar.resolve(strict=True) != sidecar:
        raise ManifestError(f'{sidecar} must be a real directory inside the job')
    # Binds the sidecar to one registration request and queue, so matching job.json bytes
    # alone never let another request or queue register a second attempt on this ID.
    claim = canonical({'request_id': request_id, 'queue_id': queue_id})
    marker = sidecar / 'registration-request.json'
    if os.path.lexists(marker):
        if marker.read_bytes() != claim:
            raise ManifestError(f'{native["id"]} is already claimed by another registration request or queue')
    else:
        if any(sidecar.iterdir()):
            raise ManifestError(f'{sidecar} holds files from an unknown registration')
        _publish_new(marker, claim, 0o444)
    path = sidecar / 'job.json'
    if os.path.lexists(path):
        if path.read_bytes() != raw:
            raise ManifestError(f'{path} holds different bytes')
        return
    _publish_new(path, raw, 0o444)
