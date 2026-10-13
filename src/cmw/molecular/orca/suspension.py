"""Bounded ORCA driver enrollment; this does not qualify a scientific engine run."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

from cmw.jobs.ownership import owner_alive
from cmw.jobs.suspension_protocol import (BINDING_FIELDS, PROTOCOL, SuspensionProtocolError,
    _directory_identity, _private_info, _read_fd, clock_descriptor, controlled_driver,
    file_identity, mpi_runtime_identity)
from .input import parse_rendered_orca_input
from .job import load_target
from .status import StageType

# An explicit bounded grammar prevents a familiar engine name from enrolling
# numerical task farms, compound input, external commands, or unknown producers.
ORDINARY_KEYWORDS = frozenset('HF RHF UHF RKS UKS PBE PBE0 B3LYP BP86 BLYP TPSS TPSSh '
    'r2SCAN r2SCAN-3c B97-3c HF-3c D3 D3BJ D4 RI RIJ RIJCOSX RIJK AutoAux '
    'NoAutoStart TightSCF VeryTightSCF NormalSCF LooseSCF SlowConv '
    'TightOpt LooseOpt NormalOpt SP Opt'.casefold().split())
BASIS = re.compile(r'(?:def2-(?:sv(?:p|\(p\))|(?:t|q)zv(?:p|pp))(?:d)?(?:/(?:j|jk|c))?|sto-3g|'
                   r'(?:aug-)?cc-pv[dtq]z(?:/(?:j|jk|c))?|6-31(?:1)?\+?g(?:\*{1,2})?)', re.I)


def describe(input_path, target_path, *, nprocs, orca_exe, runtime_path=None):
    """Inspect existing contracts without launching ORCA or creating Jobs state."""
    if os.environ.get('CMW_CAFFEINATE', '0') != '0':
        raise SuspensionProtocolError('Controlled ORCA does not qualify job-owned power assertions')
    target, _ = load_target(Path(target_path))
    if target.stage_type not in {'SP', 'OPT'}:
        raise SuspensionProtocolError('Controlled ORCA supports ordinary SP/OPT only; frequency and task farms are unqualified')
    parsed, resources = parse_rendered_orca_input(Path(input_path), StageType(target.stage_type))
    for key in ('keywords', 'blocks'):
        if parsed.calculation.get(key) != target.calculation.get(key):
            raise SuspensionProtocolError('ORCA input differs from its declared scientific target')
    if (parsed.geometry_sha256, parsed.charge, parsed.multiplicity) != (
            target.geometry_sha256, target.charge, target.multiplicity):
        raise SuspensionProtocolError('ORCA input geometry/electronic state differs from its target')
    if resources.nprocs != nprocs:
        raise SuspensionProtocolError('ORCA PAL count differs from the declared runner resource count')
    if parsed.calculation.get('blocks'):
        raise SuspensionProtocolError('Extra ORCA input blocks have no qualified suspension topology')
    for token in str(parsed.calculation['keywords']).split():
        if token.casefold() not in ORDINARY_KEYWORDS and not BASIS.fullmatch(token):
            raise SuspensionProtocolError(f'ORCA keyword has no qualified suspension topology: {token}')
    runtime = {'driver': file_identity(orca_exe), 'ranks': nprocs, 'local_only': True,
               'stage': target.stage_type, 'input_contract': 'cmw-rendered-ordinary-sp-opt-v1',
               'native_engine_qualified': False, 'sleep_qualified': False, 'power_assertion': 'none'}
    if nprocs > 1:
        if runtime_path is None:
            raise SuspensionProtocolError('Controlled ORCA PAL requires a validated runtime contract')
        value = json.loads(Path(runtime_path).read_text())
        if value.get('schema_version') != 3 or value.get('nprocs') != nprocs:
            raise SuspensionProtocolError('ORCA runtime schema/resource binding is unsupported')
        if Path(value.get('orca', {}).get('executable', '')).resolve() != Path(orca_exe).resolve():
            raise SuspensionProtocolError('ORCA runtime driver identity does not match')
        runtime.update(mpi_runtime_identity(value['mpi']['launcher'], nprocs))
        if runtime['mpi_launcher']['version'] != value['mpi']['version']:
            raise SuspensionProtocolError('ORCA MPI version differs from validated runtime')
        runtime['runtime_id'] = value['runtime_id']
        runtime['library_directories'] = value['mpi'].get('library_directories', [])
    return runtime


def _read_private_record(path):
    path = Path(path)
    if not path.is_absolute():
        raise SuspensionProtocolError('Completion record path must be absolute')
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        _private_info(info, directory=True)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            value = _read_fd(fd)
        finally:
            os.close(fd)
        current = path.parent.stat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise SuspensionProtocolError('Completion record directory changed')
        return value
    finally:
        os.close(directory)


def write_completion(path, value):
    """Publish the registered runner's drained outcome once in shell-owned scratch."""
    path = Path(path)
    if not path.is_absolute() or path.parent.resolve() != Path(os.environ['TMPDIR']).resolve():
        raise SuspensionProtocolError('Completion record must use the shell private scratch directory')
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = '.' + path.name + '.' + uuid4().hex
    try:
        _private_info(os.fstat(directory), directory=True)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, sort_keys=True, allow_nan=False)
            handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
        # A hard link publishes the complete record atomically without replacing
        # an existing record, including a stale file or symlink.
        os.link(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory,
                follow_symlinks=False)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def verify_completion(path, *, process_status, runner_pid):
    """Read-only shell handoff; exit codes alone never establish cancellation."""
    path = Path(path)
    if path.parent.resolve() != Path(os.environ['TMPDIR']).resolve():
        raise SuspensionProtocolError('Completion source differs from shell private scratch')
    context_path = Path(os.environ['CMW_SUSPENSION_CONTEXT'])
    if context_path.name != 'context.json':
        raise SuspensionProtocolError('Invalid completion context path')
    context = _read_private_record(context_path)
    registration = _read_private_record(context_path.parent / 'registration.json')
    value = _read_private_record(path)
    directory_identity = _directory_identity(context_path.parent)
    if (context.get('protocol') != PROTOCOL or context.get('backend') != 'orca-local-v1'
            or context.get('directory_identity') != {key: directory_identity[key] for key in ('device', 'inode')}
            or context.get('clock') != clock_descriptor()):
        raise SuspensionProtocolError('Completion context identity differs')
    if any(key not in context or context[key] is None or value.get(key) != context[key]
           or registration.get(key) != context[key] for key in BINDING_FIELDS):
        raise SuspensionProtocolError('Completion attempt/authority binding differs')
    runner = registration.get('runner')
    if (not isinstance(runner, dict) or not {'pid', 'birth', 'host', 'boot'} <= runner.keys()
            or value.get('runner') != runner or runner.get('pid') != runner_pid
            or not isinstance(registration.get('root'), dict)
            or not {'pid', 'birth', 'host', 'boot'} <= registration['root'].keys()
            or value.get('root') != registration['root']):
        raise SuspensionProtocolError('Completion source is not the registered child runner')
    if (value.get('source') != 'cmw-controlled-driver-drained-v1'
            or value.get('disposition') not in {'completed', 'cancelled'}
            or isinstance(value.get('exit_code'), bool) or not isinstance(value.get('exit_code'), int)
            or value['exit_code'] != process_status or not 0 <= process_status <= 255):
        raise SuspensionProtocolError('Completion disposition or process status differs')
    disposition = value['disposition']
    try:
        command = _read_private_record(context_path.parent / 'command.json')
    except FileNotFoundError:
        command = None
    if command is not None:
        if (any(command.get(key) != context[key] for key in BINDING_FIELDS)
                or isinstance(command.get('revision'), bool) or not isinstance(command.get('revision'), int)
                or command['revision'] < 1 or not isinstance(command.get('request_id'), str)
                or not command['request_id'] or command.get('operation') not in {
                    'prepare_suspend', 'suspended', 'prepare_continue', 'run', 'cancel', 'invalidate'}):
            raise SuspensionProtocolError('Latest completion control command is not authenticated')
        # Cancellation may arrive after the runner exits but before this shell
        # gate. Preserve its finalization hold without signaling an exited runner.
        if command['operation'] == 'cancel':
            disposition = 'cancelled'
        if command['operation'] == 'invalidate':
            raise SuspensionProtocolError('Completion authority was invalidated')
    if _read_private_record(context_path) != context:
        raise SuspensionProtocolError('Completion context changed')
    if not owner_alive(context['worker']) or not owner_alive(context['anchor']):
        raise SuspensionProtocolError('Completion worker or protected anchor is unavailable')
    anchor = context['anchor']['pid']
    if os.getsid(0) != anchor or os.getsid(anchor) != anchor or os.getpgid(anchor) != anchor:
        raise SuspensionProtocolError('Completion shell is outside its protected owned session')
    return disposition


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('describe', 'run', 'verify-completion'))
    parser.add_argument('--input')
    parser.add_argument('--target')
    parser.add_argument('--orca-exe')
    parser.add_argument('--nprocs', type=int)
    parser.add_argument('--runtime-contract')
    parser.add_argument('--storage', action='append', default=[])
    parser.add_argument('--completion-record')
    parser.add_argument('--process-status', type=int)
    parser.add_argument('--runner-pid', type=int)
    args = parser.parse_args(argv)
    if args.operation == 'verify-completion':
        if args.completion_record is None or args.process_status is None or args.runner_pid is None:
            parser.error('verify-completion requires record, process status and child runner PID')
    elif any(getattr(args, key) is None for key in ('input', 'target', 'orca_exe', 'nprocs')):
        parser.error('describe/run require input, target, orca-exe and nprocs')
    try:
        if args.operation == 'verify-completion':
            print(verify_completion(args.completion_record, process_status=args.process_status,
                                    runner_pid=args.runner_pid))
            return 0
        runtime = describe(args.input, args.target, nprocs=args.nprocs,
                           orca_exe=args.orca_exe, runtime_path=args.runtime_contract)
        if args.operation == 'describe':
            print(json.dumps({'protocol': 1, 'backend': 'orca-local-v1', 'runtime': runtime,
                              'deadline': 'no execution timeout configured in this supported runner',
                              'qualification': {'engine': False, 'sleep_wake': False}}))
            return 0
        storage = [str(Path(args.input).resolve().parent), str(Path(args.target).resolve().parent),
                   str(Path(args.orca_exe).resolve().parent), *args.storage]
        if args.runtime_contract:
            storage.append(str(Path(args.runtime_contract).resolve().parent))
        if runtime.get('mpi_launcher'):
            storage.extend(str(Path(item['path']).parent) for item in
                           [*runtime['launcher_executables'], *runtime['control_executables']])
            storage.extend(runtime.get('library_directories', []))
        if os.environ.get('TMPDIR'):
            storage.append(os.environ['TMPDIR'])
        result = controlled_driver([str(Path(args.orca_exe).resolve()), Path(args.input).name],
                                 backend='orca-local-v1', storage=storage, runtime=runtime,
                                 strategy='orca-tree', completion_details=bool(args.completion_record))
        if args.completion_record:
            write_completion(args.completion_record, result)
            return result['exit_code']
        return result
    except (OSError, ValueError, KeyError) as exc:
        print(f'Controlled ORCA refused: {exc}', file=sys.stderr)
        return 69


if __name__ == '__main__':
    raise SystemExit(main())
