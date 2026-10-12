"""Bounded ORCA driver enrollment; this does not qualify a scientific engine run."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

from cmw.jobs.suspension_protocol import (SuspensionProtocolError, controlled_driver,
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('describe', 'run'))
    parser.add_argument('--input', required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--orca-exe', required=True)
    parser.add_argument('--nprocs', required=True, type=int)
    parser.add_argument('--runtime-contract')
    parser.add_argument('--storage', action='append', default=[])
    args = parser.parse_args(argv)
    try:
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
        return controlled_driver([str(Path(args.orca_exe).resolve()), Path(args.input).name],
                                 backend='orca-local-v1', storage=storage, runtime=runtime,
                                 strategy='orca-tree')
    except (OSError, ValueError, KeyError) as exc:
        print(f'Controlled ORCA refused: {exc}', file=sys.stderr)
        return 69


if __name__ == '__main__':
    raise SystemExit(main())
