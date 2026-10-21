"""Attempt-scoped prepared identities verified against declared VASP staging.

Registration never launches or stages inputs. Observation checks local runner
records, not authenticated execution or scientific acceptance, and never writes.
"""
from __future__ import annotations

import math
from pathlib import Path
import time

from cmw.periodic.vasp.result_runner import (
    _CONTROLLED_KEYS, _DIGEST, _INPUTS, _KEYS, _STAGED, _parse_record, _timestamp,
)
from cmw.periodic.vasp.result_sources import SnapshotReader
from cmw.progress.models import SourceDescriptor
from .location_resolvers import vasp_runner_arguments

MAX_POSCAR_BYTES = 1024 * 1024
ORIGIN = 'attempt-staged-input'


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _snapshot(path, role, limit, *, text=False):
    reader = SnapshotReader(path, role, max_bytes=limit)
    with reader:
        content = ''.join(line.text for line in reader) if text else None
        if not text:
            for _ in reader:
                pass
    _require(reader.record['stable'] and reader.record['coverage'] == 'complete',
             f'{role} was not completely and stably observed within {limit} bytes')
    return content, reader.record


def prepare_vasp_input(job):
    """Freeze optional prepared identity at add; missing evidence cannot veto add."""
    if str(job['engine']).upper() != 'VASP':
        return None
    binding = {'schema': 1, 'attempt_id': job['attempt_id'], 'argv': job['argv'], 'issue': None}
    try:
        route = vasp_runner_arguments(job)
        if route is None:
            return None  # Opaque wrappers/layouts have no declared staging contract.
        options, source, output = route
        if '--managed-foreground' not in options or '--dry-run' in options:
            return None  # Preserve existing unverified path-only observation.
        binding.update(input=str(source), output=str(output))
        _require(source == source.resolve() and output == output.resolve(),
                 'Staging paths must be canonical without symlink traversal')
        parent = output.parent.stat()
        binding['parent_identity'] = [parent.st_dev, parent.st_ino]
        try:
            output.lstat()
        except FileNotFoundError:
            pass
        else:
            raise ValueError('Runner output already exists at registration; original attempt staging is unproven')
        _, observed = _snapshot(source / 'POSCAR', 'prepared POSCAR', MAX_POSCAR_BYTES)
        before = observed['stat_before']
        binding.update(sha256=observed['sha256'], bytes=observed['bytes_read'],
                       source_stat=[before[key] for key in ('device', 'inode', 'size', 'mtime_ns')])
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        binding['issue'] = str(exc)
    return binding


def _staging_evidence(content, binding, job, options, now):
    # Terminal failures do not invalidate a completed staging step. Only the
    # ordered prefix before launch is relevant; finalization still checks all
    # terminal fields using the same record parser and its stricter contract.
    lines = content.splitlines()
    _require('state: RUNNING' in lines, 'Runner staging has not reached its RUNNING boundary')
    prefix = '\n'.join(lines[:lines.index('state: RUNNING') + 1])
    values, positions, staged = _parse_record(prefix, _KEYS | _CONTROLLED_KEYS)
    _require(values.get('state') == ['STAGING', 'RUNNING'] and positions['state'][0] == 0,
             'Runner staging lifecycle is ambiguous')
    _require(values.get('child_status') == ['NOT STARTED'] and set(staged) == set(_INPUTS),
             'Runner staging must identify all four inputs before launch')
    scalar = {key: entries[0] for key, entries in values.items() if len(entries) == 1}
    _require(scalar.get('source') == binding['input'] and scalar.get('output') == binding['output'],
             'Runner staging paths differ from the registered attempt')
    _require(scalar.get('managed_foreground') == 'True' and scalar.get('requested_version') == '6.6.1'
             and scalar.get('restart_requested') == options.get('--restart', 'none'),
             'Runner staging mode differs from the declared contract')
    if '--controlled-suspension' in options:
        _require(scalar.get('runner_metadata_version') == '2'
                 and scalar.get('controlled_suspension_protocol') == 'cmw-suspension-v1',
                 'Controlled runner staging protocol is unavailable')
    staged_at = _timestamp(scalar.get('staging_start_utc'))
    start, end = job.get('started_at'), job.get('finished_at')
    end = now if end is None else end
    _require(all(type(value) in (int, float) and math.isfinite(value) for value in (start, end))
             and 0 <= start <= staged_at <= end <= now,
             'Runner staging timestamp is outside the recorded Jobs attempt')
    identity = staged['POSCAR']
    _require(identity == {'sha256': binding['sha256'], 'bytes': binding['bytes']},
             'Staged POSCAR byte identity differs from the prepared attempt snapshot')
    match = next(_STAGED.fullmatch(value) for value in values['staged_file'] if value.startswith('POSCAR;'))
    _require([int(value) for value in match.groups()[2:6]] == binding['source_stat'],
             'Staged POSCAR source identity differs from the registered prepared input')


def resolve_vasp_input(job, engine, *, now=None):
    """Return a pinned staged source or a qualified absence, never a substitute."""
    try:
        binding = job['progress_sources']['vasp_input']
        _require(isinstance(binding, dict) and type(binding.get('schema')) is int and binding['schema'] == 1
                 and binding.get('attempt_id') == job['attempt_id']
                 and binding.get('argv') == job['argv']
                 and engine == str(job['engine']).upper() == 'VASP',
                 'POSCAR registration belongs to a different attempt, engine or invocation')
        _require(not binding.get('issue'), binding.get('issue'))
        _require(isinstance(binding.get('sha256'), str) and _DIGEST.fullmatch(binding['sha256'])
                 and type(binding.get('bytes')) is int and 0 < binding['bytes'] <= MAX_POSCAR_BYTES,
                 'Registered POSCAR identity is invalid')
        route = vasp_runner_arguments(job)
        _require(route is not None, 'Registered staging route is unavailable')
        options, source, output = route
        _require(str(source) == binding['input'] and str(output) == binding['output']
                 and source == source.resolve() and output == output.resolve(),
                 'Registered staging paths changed or traverse a symlink')
        parent = output.parent.stat()
        _require([parent.st_dev, parent.st_ino] == binding['parent_identity'],
                 'Registered output parent identity changed')
        directory = output.stat()
        directory_identity = (directory.st_dev, directory.st_ino)
        content, _ = _snapshot(output / 'RUN_METADATA.txt', 'runner staging record', 64 * 1024, text=True)
        _staging_evidence(content, binding, job, options, time.time() if now is None else now)
        # Digest verification happens again over the exact bytes consumed by the
        # bounded reader, closing the resolve/read gap without reading outputs.
        _, observed = _snapshot(output / 'POSCAR', 'staged POSCAR', MAX_POSCAR_BYTES)
        _require(observed['sha256'] == binding['sha256'] and observed['bytes_read'] == binding['bytes'],
                 'Runtime POSCAR differs from its immutable staged-input identity')
        after, parent_after = output.stat(), output.parent.stat()
        _require((after.st_dev, after.st_ino) == directory_identity
                 and [parent_after.st_dev, parent_after.st_ino] == binding['parent_identity']
                 and output == output.resolve(), 'Staging directory identity changed during verification')
        return SourceDescriptor('poscar', str(output / 'POSCAR'),
            qualification='attempt-staged-input; local staging byte identity verified; scientific lineage unassessed',
            optional=True, root_path=str(output), root_identity=directory_identity,
            origins=(ORIGIN,), expected_sha256=binding['sha256'], expected_size=binding['bytes']), ()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, StopIteration, AttributeError) as exc:
        return None, ('Attempt POSCAR unavailable: ' + str(exc),)
