"""Dry-run backfill evidence for terminated legacy jobs (#41 Phase 4b, #76).

A reviewed inventory names each legacy job by its queue-qualified ID, the
existing job wrapper that would receive ``.cmw-manifest/`` and the labels it was
observed under. ``plan`` resolves every reference through its queue binding
(Phase 4a) and reports each manifest field as evidence, missing or conflict,
with the documents and ``legacy_alias`` receipts a reviewed ``--apply``
(Phase 4c) would publish. Sources: the bound queue record for status, times,
exit code and launch; the runner's verified staging record for input and
restart digests; the bounded SCF classifier for ``scf_result``; a parent only
from a unique restart-digest match against the inventory's own terminated jobs.
Active or unresolved jobs are deferred. Nothing is written.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

from .manifest_hashing import stable_digest
from .manifest_legacy import QUALIFIED, resolve
from .manifest_state import RESTART_ROLES, STATUS, TERMINAL, _scf, _utc
from .manifests import (ENVIRONMENT_PREFIXES, MANIFEST_DIR, SCHEMA_VERSION, VASP_INPUTS, ManifestError,
                        _require_owner, load_project, read_journal)

ENGINES = {'VASP': 'VASP', 'ORCA': 'ORCA', 'MULTIWFN': 'Multiwfn', 'COMMAND': 'Command'}
LABEL = re.compile(r'[^\x00-\x1f\x7f]{1,128}')
NODE_MAP_DEFERRED = 'the node-token to node-UUID map is deferred (#76); backfilled node fields stay null'


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


def _destination(root, wrapper, job, output):
    """The new sidecar path: only the job's working directory or the directory holding its run output."""
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
        return _field(None, 'inventory', f'{path / MANIFEST_DIR} already exists; backfill never overwrites',
                      conflict=True)
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


def _candidate(root, project, entry, manifested, volume_uuid_of):
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
    row['fields'] = {'destination': _destination(root, entry['wrapper'], job, output),
                     **job_fields, **state_fields}
    if reference in manifested:
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


def _order(reference):
    match = QUALIFIED.fullmatch(reference)
    return int(match[3]), int(match[4]), match[1], match[2]


def plan(store, root, inventory, *, volume_uuid_of=None):
    """The dry-run backfill report for a reviewed inventory file; writes nothing."""
    root, project = load_project(root)
    _require_owner(store, project)
    entries = load_inventory(inventory, project['project_key'])
    manifested = {record['payload']['id'] for record in read_journal(root) if record['event'] == 'manifest'}
    rows, candidates = [], []
    for entry in sorted(entries, key=lambda item: _order(item['reference'])):
        row, candidate = _candidate(root, project, entry, manifested, volume_uuid_of)
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
