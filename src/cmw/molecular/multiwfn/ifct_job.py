"""Prepare and validate one queue-managed, two-fragment IFCT revalidation."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from cmw.core.provenance import file_hash
from cmw.molecular.excited_states import ExcitedStateRecord
from cmw.molecular.orca.excited_states import parse_orca_tda_excited_states_file
from cmw.molecular.stacking.hole_electron import FragmentDefinition
from cmw.molecular.multiwfn.adapter import MultiwfnOutputSpec, normalize_output_paths
from cmw.molecular.multiwfn.excited_states import (
    Multiwfn2026IfctRenderer, build_excited_state_command_spec,
    parse_multiwfn2026_ifct_session_file,
)


def prepare(spec_path: Path, attempt: Path) -> dict:
    spec = json.loads(spec_path.read_text())
    runtime = json.loads((attempt / 'runtime.json').read_text())['runtime']
    native = parse_orca_tda_excited_states_file(Path(spec['orca_output']))
    state = native.state(spec['spin_manifold'], spec['state_index'])
    fragments = tuple(FragmentDefinition(f['label'], tuple(f['indices_zero_based']))
                      for f in spec['fragments'])
    protocol = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    rendered = Multiwfn2026IfctRenderer().render(
        state, orca_output_path=Path(spec['orca_output']),
        source_wavefunction_path=Path(spec['wavefunction']),
        fragment_definitions=fragments, atom_count=spec['atom_count'],
        scientific_protocol_hash=protocol,
        source_geometry_hash=file_hash(Path(spec['geometry'])),
        execution_layout={'working_directory': str(attempt.resolve()),
                          'attempt_identifier': attempt.name},
        execution_attempt={'attempt_id': attempt.name}, settings_identity=runtime)
    menu = attempt / 'menu.in'
    menu.write_text(rendered.stdin_text)
    command = build_excited_state_command_spec(
        rendered, runtime=runtime, attempt_directory=attempt, stdin_path=menu)
    record = {'rendered': rendered.to_dict(), 'state': state.to_dict(),
              'runtime': runtime, 'spec': spec, 'command': command.to_dict()}
    (attempt / 'plan.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def finalize(attempt: Path, exit_code: int) -> dict:
    plan = json.loads((attempt / 'plan.json').read_text())
    if exit_code != 0:
        raise ValueError(f'Multiwfn exited with status {exit_code}')
    rendered = plan['rendered']
    for key in ('source_wavefunction_identity', 'orca_output_identity'):
        identity = rendered[key]
        if file_hash(Path(identity['path'])) != identity['sha256']:
            raise ValueError('Source changed during IFCT execution')
    if file_hash(attempt / 'menu.in') != rendered['stdin_sha256']:
        raise ValueError('IFCT menu changed during execution')
    runtime = plan['runtime']
    if file_hash(Path(runtime['settings_path'])) != runtime['settings_sha256']:
        raise ValueError('Run-local settings changed during execution')
    outputs = tuple(MultiwfnOutputSpec.from_mapping(v) for v in rendered['outputs'])
    normalize_output_paths(attempt, outputs)
    session = next(o for o in outputs if o.role == 'session_log')
    parsed = parse_multiwfn2026_ifct_session_file(
        attempt / session.output_path,
        expected_state=ExcitedStateRecord.from_mapping(plan['state']),
        fragment_labels=[f['label'] for f in plan['spec']['fragments']])
    if parsed.state_evidence.parallel_threads != runtime['requested_nthreads']:
        raise ValueError('Native thread count differs from requested resources')
    # The standard shell launcher records its short-path source alias.
    alias = dict(line.split('=', 1) for line in
                 (attempt / 'multiwfn-runtime-alias.txt').read_text().splitlines())
    if alias.get('source_target') != rendered['source_wavefunction_identity']['path']:
        raise ValueError('Runtime wavefunction target differs from plan')
    if parsed.state_evidence.loaded_source_wavefunction_path != alias.get('source'):
        raise ValueError('Native loaded wavefunction differs from runtime alias')
    result = {'validation_status': 'PASSED', 'process_exit_code': exit_code,
              'analysis': 'IFCT numerical revalidation', 'parsed': asdict(parsed),
              'plan': str(attempt / 'plan.json')}
    (attempt / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--spec', type=Path, required=True)
    prep.add_argument('--attempt', type=Path, required=True)
    final = sub.add_parser('finalize')
    final.add_argument('--attempt', type=Path, required=True)
    final.add_argument('--exit-code', type=int, required=True)
    args = parser.parse_args()
    if args.operation == 'prepare':
        result = prepare(args.spec, args.attempt)
    else:
        result = finalize(args.attempt, args.exit_code)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
