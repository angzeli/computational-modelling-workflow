"""Backfill of terminated legacy jobs: dry-run evidence and reviewed apply (#41 Phases 4b-4c, #76).

A reviewed inventory names each legacy job by its queue-qualified ID, the
existing job wrapper that would receive ``.cmw-manifest/`` and the labels it was
observed under. ``plan`` resolves every reference through its queue binding
(Phase 4a) and reports each manifest field as evidence, missing or conflict,
with the documents and ``legacy_alias`` receipts a reviewed ``--apply``
(Phase 4c) would publish. Sources: the bound queue record for status, times,
exit code and launch; the runner's verified staging record for input and
restart digests; the bounded SCF classifier for ``scf_result``; a parent only
from a unique restart-digest match against the inventory's own terminated jobs.
Active or unresolved jobs are deferred. The dry-run writes nothing.

``apply`` takes the reviewed dry-run report, plans again from the same sources and
refuses everything unless each reviewed ready job is still ready with identical
documents. It then publishes only new ``.cmw-manifest/`` files (never replacing one),
anchors each job.json with a ``manifest`` receipt (binding null) and appends one
``legacy_alias`` receipt per label. Request UUIDs are derived from the project,
reference and label, so a retry after an interruption resumes and a completed
apply is a no-op. Existing files, paths, permissions and queue records are not touched.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID, uuid5

from .manifest_hashing import stable_digest
from .manifest_legacy import QUALIFIED, order_key, resolve
from .manifest_state import RESTART_ROLES, STATUS, TERMINAL, _scf, _utc
from .manifests import (ENVIRONMENT_PREFIXES, MANIFEST_DIR, SCHEMA_VERSION, VASP_INPUTS, ManifestError,
                        _publish_new, _require_owner, append_journal, canonical, load_project, read_journal)

ENGINES = {'VASP': 'VASP', 'ORCA': 'ORCA', 'MULTIWFN': 'Multiwfn', 'COMMAND': 'Command'}
LABEL = re.compile(r'[^\x00-\x1f\x7f]{1,128}')
NODE_MAP_DEFERRED = 'the node-token to node-UUID map is deferred (#76); backfilled node fields stay null'
# Fixed namespace for request UUIDs derived from (project, reference[, label]): retries are idempotent.
REQUESTS = UUID('6d1f6c2e-3c1b-5f0e-9a52-4b8e7f0c2d19')
# Published sidecar files and modes. job.json and state.json are frozen evidence;
# annotations.json is the future append target for human judgement.
SIDECAR = (('job.json', 'job', 0o444), ('state.json', 'state', 0o444), ('annotations.json', 'annotations', 0o644))


def load_inventory(path, project_key):
    """The reviewed inventory's job entries; a malformed or ambiguous inventory is refused whole."""
    try:
        inventory = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ManifestError(f'Backfill inventory is unreadable: {exc}') from exc
    if not isinstance(inventory, dict) or set(inventory) != {'schema_version', 'project_key', 'jobs'}:
        raise ManifestError('Backfill inventory must have exactly schema_version, project_key and jobs')
    if inventory['schema_version'] != 1 or inventory['project_key'] != project_key:
        raise ManifestError('Backfill inventory schema or project key does not match')
    jobs = inventory['jobs']
    if not isinstance(jobs, list) or not jobs:
        raise ManifestError('Backfill inventory jobs must be a nonempty list')
    references, wrappers = set(), set()
    for entry in jobs:
        if not isinstance(entry, dict) or set(entry) != {'reference', 'wrapper', 'labels'}:
            raise ManifestError('Each inventory job has exactly reference, wrapper and labels')
        reference, wrapper, labels = entry['reference'], entry['wrapper'], entry['labels']
        if not isinstance(reference, str) or QUALIFIED.fullmatch(reference) is None:
            raise ManifestError(f'Inventory reference {reference!r} is not queue-qualified <node>/<queue>:J<n>.<m>')
        if not isinstance(wrapper, str) or not os.path.isabs(wrapper):
            raise ManifestError(f'{reference}: the wrapper must be an absolute path')
        if (not isinstance(labels, list) or len(set(labels)) != len(labels)
                or not all(isinstance(label, str) and LABEL.fullmatch(label) for label in labels)):
            raise ManifestError(f'{reference}: labels must be distinct strings of 1-128 characters '
                                'without control characters')
        if reference in references:
            raise ManifestError(f'{reference} is listed twice')
        if os.path.normpath(wrapper) in wrappers:
            raise ManifestError(f'Wrapper {wrapper} is listed for two jobs')
        references.add(reference)
        wrappers.add(os.path.normpath(wrapper))
    return jobs


def _field(value, source=None, detail=None, *, conflict=False):
    state = 'conflict' if conflict else 'missing' if value is None else 'evidence'
    return {'state': state, 'value': None if conflict else value, 'source': source, 'detail': detail}


def _destination(root, wrapper, job, output, resume=False):
    """The new sidecar path: only the job's working directory or the directory holding its run output.

    ``resume`` admits a real sidecar directory an interrupted apply already created; apply then
    verifies every file in it byte for byte.
    """
    path = Path(wrapper)
    try:
        info, resolved = path.lstat(), path.resolve(strict=True)
    except OSError as exc:
        return _field(None, 'inventory', f'wrapper is unavailable: {exc}', conflict=True)
    if not stat.S_ISDIR(info.st_mode) or resolved != path:
        return _field(None, 'inventory', 'wrapper must be a real directory, not a symlink or a path through one',
                      conflict=True)
    if path == root / 'jobs' or root / 'jobs' in path.parents:
        return _field(None, 'inventory', "wrapper is inside the project's native jobs/ tree", conflict=True)
    allowed = {Path(job['cwd'])} | ({output.parent} if output is not None else set())
    if path not in allowed:
        return _field(None, 'inventory', "wrapper is neither the job's recorded working directory nor the "
                      'directory holding its runner output', conflict=True)
    if os.path.lexists(path / MANIFEST_DIR):
        sidecar = path / MANIFEST_DIR
        if resume and stat.S_ISDIR(sidecar.lstat().st_mode) and sidecar.resolve() == sidecar:
            return _field(str(sidecar), 'inventory', 'created by an earlier apply of this report')
        return _field(None, 'inventory', f'{sidecar} already exists; backfill never overwrites', conflict=True)
    return _field(str(path / MANIFEST_DIR), 'inventory')


def _route(job):
    from .location_resolvers import vasp_runner_arguments
    try:
        return vasp_runner_arguments(job)
    except (KeyError, TypeError, ValueError):
        return None


def _staged(job, engine, route):
    """Runner-staged identities, or (None, reason) when the attempt's staging is not verified."""
    from .input_sources import staged_identities
    if engine != 'VASP':
        return None, 'not a VASP job'
    if route is None:
        return None, 'no declared managed runner route'
    try:
        return staged_identities(job), None
    except ValueError as exc:
        return None, f'runner staging is not verified: {exc}'


def _job_fields(project, reference, job, engine, route, staged, issue):
    from cmw.periodic.vasp.result_runner import RESTART_FILES
    fields = {}
    fields['job.created_at'] = _field(_utc(job.get('enqueued_at')), 'queue enqueued_at')
    for name in ('authoring_node', 'target_node', 'id_owner_node'):
        fields[f'job.{name}'] = _field(None, None, NODE_MAP_DEFERRED)
    fields['job.engine'] = (_field(engine, 'queue') if engine is not None else
                            _field(None, 'queue', f'queue engine {job.get("engine")!r} is not a manifest engine',
                                   conflict=True))
    for name in VASP_INPUTS:
        digest = staged[name]['sha256'] if staged else None
        fields[f'job.inputs.{name}'] = _field(digest, 'runner staging record' if staged else None, issue)
    fields['job.potcar'] = _field(None, None, 'POTCAR dataset identities are not in the queue or staging record; '
                                  'current files are not trusted as the executed input')
    for name in ('preparation_record', 'executable', 'runtime_fingerprint'):
        fields[f'job.{name}'] = _field(None, None, 'not recorded for legacy jobs')
    argv = job.get('argv')
    fields['job.launch.argv'] = _field(list(argv) if isinstance(argv, list) and argv else None, 'queue')
    ranks = (job.get('resources') or {}).get('mpi_ranks')
    fields['job.launch.mpi_ranks'] = _field(ranks if type(ranks) is int else None, 'queue')
    env = job.get('env')
    selected = ({key: value for key, value in env.items() if key.startswith(ENVIRONMENT_PREFIXES)}
                if isinstance(env, dict) else None)
    fields['job.launch.environment'] = _field(selected, 'queue (allowed prefixes only)')
    roles = RESTART_FILES.get(route[0].get('--restart', 'none'), ()) if engine == 'VASP' and route else ()
    restart = {role: staged[role] for role in roles} if staged else None
    for role in roles:
        fields[f'job.restart_inputs.{role}'] = _field(restart[role]['sha256'] if restart else None,
                                                      'runner staging record' if restart else None, issue)
    if engine == 'VASP' and route is None:
        fields['job.restart_inputs'] = _field(None, None, 'no declared runner route; restart inputs are unknown')
    document = {
        'schema_version': SCHEMA_VERSION, 'id': reference, 'project': project['project_key'],
        'provenance': 'backfilled', 'created_at': fields['job.created_at']['value'],
        'authoring_node': None, 'target_node': None, 'id_owner_node': None, 'engine': engine,
        'inputs': {name: fields[f'job.inputs.{name}']['value'] for name in VASP_INPUTS},
        'potcar': None, 'preparation_record': None, 'executable': None, 'runtime_fingerprint': None,
        'launch': {'argv': fields['job.launch.argv']['value'], 'mpi_ranks': fields['job.launch.mpi_ranks']['value'],
                   'environment': selected},
        'parent': None, 'parent_inferred': False,
        'restart_inputs': {role: fields[f'job.restart_inputs.{role}']['value'] for role in roles},
    }
    return fields, document, restart


def _unattributed_scf(issue):
    marker = f"run output is not attributed to this attempt: {issue}"[:512]
    return {'scf_result': 'unknown', 'last_step': None, 'ionic_step': None, 'energy_ev': None,
            'delta_energy_ev': None, 'total_magnetisation_mu_b': None, 'restart_outputs': {},
            'hashes_pending': False, 'evidence': [{'source': 'queue', 'marker': marker, 'line': None}]}


def _present(output):
    """Nonempty regular restart files in a run output whose attempt binding did not verify."""
    found = {}
    for role in RESTART_ROLES if output is not None else ():
        try:
            info = (output / role).lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_size > 0:
            found[role] = output / role
    return found


def _state_fields(project, reference, job, engine, staged, issue):
    fields = {}
    native = job['status']
    # Output-directory facts belong to this attempt only when its staging verifies, which
    # also proves the wrapper was not replaced at the same path since registration.
    scf = (_unattributed_scf(issue) if engine == 'VASP' and staged is None else
           _scf({**job, 'engine': engine or job.get('engine')}))
    fields['state.writer_node'] = _field(None, None, NODE_MAP_DEFERRED)
    fields['state.status'] = _field(STATUS.get(native), 'queue')
    fields['state.native_status'] = _field(native, 'queue')
    fields['state.started_at'] = _field(_utc(job.get('started_at')), 'queue')
    fields['state.ended_at'] = _field(_utc(job.get('finished_at')), 'queue')
    exit_code = job.get('exit_code') if type(job.get('exit_code')) is int else None
    fields['state.exit_code'] = _field(exit_code, 'queue')
    fields['state.cancel_mode'] = _field(None, None, 'no supported cancellation mode is recorded')
    markers = '; '.join(item['marker'] for item in scf['evidence']) or 'no positive classifier rule'
    classified = scf['scf_result'] != 'unknown'
    fields['state.scf_result'] = {**_field(scf['scf_result'], 'SCF classifier'),
                                  **({} if classified else {'state': 'missing', 'detail': markers})}
    for name in ('last_step', 'ionic_step', 'energy_ev', 'delta_energy_ev', 'total_magnetisation_mu_b'):
        fields[f'state.{name}'] = _field(scf[name], 'SCF classifier')
    for role in scf['restart_outputs']:
        fields[f'state.restart_outputs.{role}'] = _field(None, None, 'not hashed: no inventory job restarted '
                                                         'from a file of this size')
    fields['state.binding'] = _field(None, None, NODE_MAP_DEFERRED)
    document = {
        'schema_version': SCHEMA_VERSION, 'id': reference, 'project': project['project_key'],
        'provenance': 'backfilled', 'revision': 0, 'writer_node': None,
        'status': STATUS.get(native), 'native_status': native, 'started_at': fields['state.started_at']['value'],
        'ended_at': fields['state.ended_at']['value'], 'exit_code': exit_code, 'cancel_mode': None,
        **{key: scf[key] for key in ('scf_result', 'last_step', 'ionic_step', 'energy_ev', 'delta_energy_ev',
                                     'total_magnetisation_mu_b', 'evidence')},
        'restart_outputs': dict.fromkeys(scf['restart_outputs']), 'hashes_pending': False, 'binding': None,
    }
    return fields, document, set(scf['restart_outputs'])


def _candidate(root, project, entry, manifested, volume_uuid_of, resume):
    reference = entry['reference']
    row = {'reference': reference, 'state': 'deferred', 'reasons': [], 'wrapper': entry['wrapper'],
           'aliases': [{'label': label, 'target': reference} for label in entry['labels']],
           'fields': {}, 'documents': None}
    try:
        job = resolve(root, reference, volume_uuid_of=volume_uuid_of)['job']
    except ManifestError as exc:
        row['reasons'].append(f'unresolved: {exc}')
        return row, None
    if job.get('status') not in TERMINAL:
        row['reasons'].append(f'queue status {job.get("status")!r} is not terminal; only terminated jobs are '
                              'backfilled')
        return row, None
    engine = ENGINES.get(str(job.get('engine')).upper())
    route = _route(job) if engine == 'VASP' else None
    output = route[2] if route else None
    staged, issue = _staged(job, engine, route)
    job_fields, job_document, restart = _job_fields(project, reference, job, engine, route, staged, issue)
    state_fields, state_document, outputs = _state_fields(project, reference, job, engine, staged, issue)
    row['fields'] = {'destination': _destination(root, entry['wrapper'], job, output, resume),
                     **job_fields, **state_fields}
    if reference in manifested and not resume:  # apply verifies its own receipts instead.
        row['fields']['manifest'] = _field(None, 'integrity journal', 'a manifest receipt already exists for '
                                           f'{reference}', conflict=True)
    row['documents'] = {'job': job_document, 'state': state_document,
                        'annotations': {'schema_version': SCHEMA_VERSION, 'id': reference,
                                        'project': project['project_key'], 'provenance': 'backfilled',
                                        'revision': 0, 'events': []}}
    return row, {'row': row, 'restart': restart, 'outputs': {role: output / role for role in outputs},
                 'unverified': _present(output) if staged is None else {}}


def _infer_parents(candidates):
    """Set a parent only where every staged restart digest matches one inventory job's bound output.

    An output whose attempt binding did not verify is compared too, but never attributed:
    a match there leaves the parent unresolved.
    """
    hashed = {}
    for child in candidates:
        if not child['restart']:
            continue
        matches, unknown = {}, []
        for role, identity in child['restart'].items():
            found = set()
            for other in candidates:
                verified = role in other['outputs']
                path = other['outputs'].get(role) or other['unverified'].get(role)
                if other is child or path is None:
                    continue
                key = (other['row']['reference'], role)
                try:
                    if key not in hashed and path.lstat().st_size != identity['bytes']:
                        continue  # A different size cannot match; multi-GiB files are hashed only when needed.
                    if key not in hashed:
                        hashed[key] = stable_digest(path)
                except OSError as exc:
                    hashed[key] = (None, f'is unreadable: {exc.strerror or exc}')
                digest, problem = hashed[key]
                if digest is None:
                    unknown.append(f'{key[0]} {role} {problem}')
                elif digest == identity['sha256'] and verified:
                    found.add(key[0])
                elif digest == identity['sha256']:
                    unknown.append(f'{key[0]} {role} matches, but that run output is not verified as its attempt\'s')
            matches[role] = found
        parents = set().union(*matches.values())
        fields, document = child['row']['fields'], child['row']['documents']['job']
        if unknown:
            fields['job.parent'] = _field(None, 'restart digests', 'a same-size restart source is unresolved: '
                                          + '; '.join(unknown), conflict=True)
        elif len(parents) == 1 and all(len(found) == 1 for found in matches.values()):
            parent = parents.pop()
            fields['job.parent'] = _field(parent, 'restart digests', 'every staged restart file matches this '
                                          "job's bound output (byte lineage, not checkpoint validity)")
            document.update(parent=parent, parent_inferred=True)
        elif not parents:
            fields['job.parent'] = _field(None, 'restart digests', "no inventory job's bound restart output "
                                          'matches the staged digests')
        else:
            found = ', '.join(f'{role}: {sorted(found) or "none"}' for role, found in sorted(matches.items()))
            fields['job.parent'] = _field(None, 'restart digests', f'restart digests do not name one parent '
                                          f'({found})', conflict=True)
    for (reference, role), (digest, problem) in hashed.items():
        other = next(item for item in candidates if item['row']['reference'] == reference)
        if role not in other['outputs']:
            continue  # Compared only; an unverified output is not attributed to its job.
        name = f'state.restart_outputs.{role}'
        other['row']['fields'][name] = (_field(digest, 'stable SHA-256 of the bound output') if digest else
                                        _field(None, 'stable SHA-256 of the bound output', f'{role} {problem}'))
        other['row']['documents']['state']['restart_outputs'][role] = digest


def plan(store, root, inventory, *, volume_uuid_of=None, resume=False):
    """The dry-run backfill report for a reviewed inventory file; writes nothing."""
    root, project = load_project(root)
    _require_owner(store, project)
    entries = load_inventory(inventory, project['project_key'])
    manifested = {record['payload']['id'] for record in read_journal(root) if record['event'] == 'manifest'}
    rows, candidates = [], []
    for entry in sorted(entries, key=lambda item: order_key(item['reference'])):
        row, candidate = _candidate(root, project, entry, manifested, volume_uuid_of, resume)
        rows.append(row)
        if candidate is not None:
            candidates.append(candidate)
    for candidate in candidates:
        if not candidate['restart']:
            candidate['row']['fields']['job.parent'] = _field(None, None, 'no verified staged restart input')
    _infer_parents(candidates)
    for row in rows:
        if row['documents'] is not None:
            conflicts = [name for name, field in row['fields'].items() if field['state'] == 'conflict']
            row['state'] = 'conflict' if conflicts else 'ready'
            row['reasons'] = [f'{name}: {row["fields"][name]["detail"]}' for name in conflicts]
    totals = {state: sum(row['state'] == state for row in rows) for state in ('ready', 'conflict', 'deferred')}
    return {'project': project['project_key'], 'root': str(root), 'mode': 'dry-run', 'written': False,
            'totals': totals, 'jobs': rows}


def request_for(project_key, reference, label=None):
    """The stable request UUID of a job's manifest receipt, or of one of its alias receipts."""
    name = f'{project_key}\0{reference}\0' + ('manifest' if label is None else f'alias\0{label}')
    return uuid5(REQUESTS, name).hex


def _reviewed(path, project_key):
    try:
        report = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ManifestError(f'Reviewed backfill report is unreadable: {exc}') from exc
    if (not isinstance(report, dict) or report.get('mode') != 'dry-run' or report.get('project') != project_key
            or not isinstance(report.get('jobs'), list)
            or not all(isinstance(row, dict) and isinstance(row.get('reference'), str) for row in report['jobs'])):
        raise ManifestError('The reviewed report must be the --json output of a backfill dry-run of this project')
    return report


def _changed(reviewed, fresh):
    """Why a reviewed ready job cannot be applied now, or None."""
    if fresh['state'] != 'ready':
        return f'it is now {fresh["state"]} ({"; ".join(fresh["reasons"])})'
    destination = (reviewed.get('fields') or {}).get('destination') or {}
    if (reviewed.get('documents') != fresh['documents'] or reviewed.get('aliases') != fresh['aliases']
            or destination.get('value') != fresh['fields']['destination']['value']):
        return 'its sources or documents changed since the reviewed dry-run'
    return None


def _expected(row):
    return {name: canonical(row['documents'][document]) for name, document, _ in SIDECAR}


def _existing_issue(path, raw, mode):
    """Why an existing sidecar file cannot stand for its reviewed publication, or None."""
    try:
        info = path.lstat()
        same = stat.S_ISREG(info.st_mode) and path.read_bytes() == raw
    except OSError:
        return f'{path} is unreadable'
    if not same:
        return f'{path} differs from the reviewed document'
    if not mode & 0o222 and info.st_mode & 0o222:  # Frozen files must still be read-only.
        return f'{path} is writable (mode {stat.S_IMODE(info.st_mode):o}); it is never anchored that way'
    return None


def _published_issue(records, project_key, row):
    """Why an earlier receipt or sidecar file conflicts with publishing this reviewed job, or None."""
    reference, expected = row['reference'], _expected(row)
    payload = {'id': reference, 'job_sha256': hashlib.sha256(expected['job.json']).hexdigest(), 'binding': None}
    for record in records:
        if record['event'] == 'manifest' and record['payload'].get('id') == reference and (
                record['request_uuid'] != request_for(project_key, reference) or record['payload'] != payload):
            return f'journal record {record["sequence"]} already anchors a different job.json'
    sidecar = Path(row['fields']['destination']['value'])
    if not os.path.lexists(sidecar):
        return None
    modes = {name: mode for name, _, mode in SIDECAR}
    for entry in sidecar.iterdir():
        if entry.name in expected:
            issue = _existing_issue(entry, expected[entry.name], modes[entry.name])
            if issue:
                return issue
        elif not any(entry.name.startswith(f'.{name}.') for name in expected):  # An interrupted temp file is kept.
            return f'{sidecar} holds {entry.name}, which no apply of this report writes'
    return None


def _publish(root, project_key, row):
    reference, expected = row['reference'], _expected(row)
    sidecar = Path(row['fields']['destination']['value'])
    try:
        sidecar.mkdir()
    except FileExistsError:
        pass
    if not stat.S_ISDIR(sidecar.lstat().st_mode) or sidecar.resolve(strict=True) != sidecar:
        raise ManifestError(f'{sidecar} must be a real directory')
    published = []
    for name, _, mode in SIDECAR:
        path = sidecar / name
        if os.path.lexists(path):
            issue = _existing_issue(path, expected[name], mode)
            if issue:
                raise ManifestError(f'{issue}; it changed during apply and is never replaced')
            continue
        _publish_new(path, expected[name], mode)
        published.append(name)

    def admit(records):
        if any(record['event'] == 'manifest' and record['payload'].get('id') == reference for record in records):
            raise ManifestError(f'{reference} is already anchored by another manifest receipt')

    payload = {'id': reference, 'job_sha256': hashlib.sha256(expected['job.json']).hexdigest(), 'binding': None}
    manifest = append_journal(root, project_key, 'manifest', request_for(project_key, reference), payload, admit)
    aliases = [append_journal(root, project_key, 'legacy_alias', request_for(project_key, reference, alias['label']),
                              alias)['sequence'] for alias in row['aliases']]
    return {'reference': reference, 'destination': str(sidecar), 'published': published,
            'manifest_sequence': manifest['sequence'], 'alias_sequences': aliases}


def apply(store, root, inventory, report, *, volume_uuid_of=None):
    """Publish the ready jobs of a reviewed dry-run report after planning them again from their sources.

    Every check happens before the first write: a changed source, a different inventory, a
    foreign receipt or a differing sidecar file refuses the whole apply.
    """
    root, project = load_project(root)
    key = project['project_key']
    reviewed = _reviewed(report, key)
    fresh = plan(store, root, inventory, volume_uuid_of=volume_uuid_of, resume=True)
    if [row['reference'] for row in reviewed['jobs']] != [row['reference'] for row in fresh['jobs']]:
        raise ManifestError('The reviewed report covers different jobs than this inventory; nothing was written')
    records = read_journal(root)
    selected, refusals = [], []
    for old, new in zip(reviewed['jobs'], fresh['jobs']):
        if old.get('state') != 'ready':
            continue
        reason = _changed(old, new) or _published_issue(records, key, new)
        if reason:
            refusals.append(f'{new["reference"]}: {reason}')
        else:
            selected.append(new)
    if refusals:
        raise ManifestError('Nothing was written; review a new dry-run. ' + ' | '.join(refusals))
    applied = [_publish(root, key, row) for row in selected]
    skipped = [{'reference': row['reference'], 'state': row.get('state')} for row in reviewed['jobs']
               if row.get('state') != 'ready']
    return {'project': key, 'root': str(root), 'mode': 'apply', 'applied': applied, 'skipped': skipped}


def apply_text(result):
    lines = [f'{result["project"]} backfill apply: {len(result["applied"])} reviewed job(s) published, '
             f'{len(result["skipped"])} not ready in the reviewed report and skipped']
    for item in result['applied']:
        files = ', '.join(item['published']) or 'no new files (already published)'
        lines.append(f'{item["reference"]}  {item["destination"]}  ({files}; manifest receipt '
                     f'{item["manifest_sequence"]}, {len(item["alias_sequences"])} alias receipt(s))')
    lines.extend(f'{item["reference"]}  skipped ({item["state"]})' for item in result['skipped'])
    return '\n'.join(lines)


def summary_text(report):
    totals = report['totals']
    lines = [f'{report["project"]} backfill dry-run: {totals["ready"]} ready, {totals["conflict"]} conflict, '
             f'{totals["deferred"]} deferred; nothing was written']
    for row in report['jobs']:
        lines.append(f'{row["reference"]}  {row["state"]}')
        lines.extend(f'  {reason}' for reason in row['reasons'])
        if row['documents'] is None:
            continue
        destination = row['fields']['destination']
        if destination['state'] == 'evidence':
            lines.append(f'  destination {destination["value"]}')
        parent = row['fields']['job.parent']
        if parent['state'] == 'evidence':
            lines.append(f'  parent {parent["value"]} (inferred from restart digests)')
        state = row['documents']['state']
        lines.append(f'  status {state["status"]} · scf_result {state["scf_result"]}')
        missing = [name for name, field in row['fields'].items() if field['state'] == 'missing']
        if missing:
            lines.append(f'  missing: {", ".join(missing)}')
        if row['aliases']:
            lines.append(f'  aliases: {", ".join(alias["label"] for alias in row["aliases"])}')
    return '\n'.join(lines)
