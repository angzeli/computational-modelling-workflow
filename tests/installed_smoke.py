"""Copied outside the checkout to test a fresh installation and owned fake jobs.

Only synthetic test helpers accompany this file. Its observers use fixture PIDs
and fixed idle host samples; every cmw module comes from the installation under test.
"""
import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

import cmw
import cmw.adapters.hof.execution
import cmw.structure.conversion
from cmw.jobs import runtime
from cmw.jobs.ownership import owner_alive
from cmw.jobs.store import ACTIVE, Store
from tests.jobs.isolated_runtime import environment, install
from tests.jobs.lifecycle_evidence import capture


def wait_for(predicate, seconds=12):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('Installed synthetic lifecycle timed out')


def command(argv, expected=0):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=15, env=environment())
    assert result.returncode == expected, (argv, result.returncode, result.stderr[-4000:])
    return result.stdout+result.stderr


def lifecycle(root, mode):
    root.mkdir()
    store = Store(root/'state')
    gates, owners = [], []
    started = time.time()
    with install():
        try:
            if mode == 'bounded-sharing':
                store.configure_sharing(mode, cpu_budget=2, memory_gib=2,
                                        cpu_reserve=0, min_available_gib=0)
            for index in range(2):
                cwd = root/str(index)
                cwd.mkdir()
                gate, ready = cwd/'release', cwd/'ready'
                gates.append(gate)
                code = ("from pathlib import Path; import time; "
                        f"Path({str(ready)!r}).touch(); gate=Path({str(gate)!r}); "
                        "deadline=time.monotonic()+15\n"
                        "while not gate.exists() and time.monotonic()<deadline: time.sleep(.02)\n"
                        "assert gate.exists(), 'fixture deadline exceeded'\n")
                declaration = {}
                if mode == 'bounded-sharing':
                    declaration = ({'role': 'auxiliary', 'independent': True,
                                    'resource_contract': 'trusted-declared'} if index
                                   else {'allow_auxiliary': True})
                store.add(argv=[sys.executable, '-c', code], cwd=cwd,
                          name=f'installed-synthetic-{index}', cpus=1, memory_gib=1, **declaration)
            runtime.start(store)
            wait_for(lambda: (root/'0/ready').exists())
            if mode == 'bounded-sharing':
                wait_for(lambda: (root/'1/ready').exists())
                assert [job['status'] for job in store.snapshot()['jobs']] == ['Run', 'Run']
            else:
                assert not (root/'1/ready').exists()
                assert store.snapshot()['jobs'][1]['status'] == 'Queue'
            for gate in gates:
                gate.touch()
            wait_for(lambda: all(job['status'] == 'Done' for job in store.snapshot()['jobs']))
            state = store.snapshot()
            if mode == 'sequential':
                assert state['jobs'][0]['finished_at'] <= state['jobs'][1]['started_at']
            assert state['schema'] == 2
        except BaseException as exc:
            try:
                capture(store, root/'failure', outcome=str(exc), started_at=started)
            except Exception as capture_error:
                print(f'Additional failure capture unavailable: {capture_error}', file=sys.stderr)
            raise
        finally:
            for gate in gates:
                gate.touch()
            if store.path.exists():
                store.dispatch(False)
                state = store.snapshot()
                owners = [state['controller'].get('owner')]
                for job in state['jobs']:
                    owners.extend((job.get('worker'), job.get('group')))
                try:
                    wait_for(lambda: not any(job['status'] in ACTIVE for job in store.snapshot()['jobs']))
                finally:
                    state = store.snapshot()
                    owners.append(state['controller'].get('owner'))
                    for job in state['jobs']:
                        owners.extend((job.get('worker'), job.get('group')))
                    runtime.stop(store)
                    try:
                        for process in list(runtime._CHILDREN):
                            process.wait(timeout=8)
                    finally:
                        runtime.reap_detached()
                        wait_for(lambda: not any(owner_alive(owner) for owner in owners if owner))
                assert not store.snapshot()['controller']['online']
                assert not runtime._CHILDREN
    return {'mode': mode, 'statuses': ['Done', 'Done'], 'live_recorded_owners_after_cleanup': 0}


def preparation_surface(root, cli):
    """Exercise installed preparation commands with invented data, without Jobs."""
    root.mkdir()
    sources, project, scratch = root/'sources', root/'project', root/'scratch'
    for directory in (sources, project, scratch):
        directory.mkdir()
    library = sources/'library'
    (library/'H').mkdir(parents=True)
    potential = library/'H'/'POTCAR'
    potential.write_bytes(b'TITEL = PAW_PBE H 01Jan2001\nVRHFIN = H: invented\n'
                          b'ENTIRELY INVENTED TEST DATA\nEnd of Dataset\n')
    structure = sources/'source.POSCAR'
    structure.write_text('invented two-block model\n1\n6 0 0\n0 6 0\n0 0 6\n'
                         'H H\n1 1\nDirect\n0 0 0\n0.5 0.5 0.5\n')
    molecule = sources/'molecule.xyz'
    molecule.write_text('2\ninvented molecule\nH 0 0 0\nH 0 0 0.75\n')
    periodic_xyz = sources/'periodic.xyz'
    periodic_xyz.write_text('1\nLattice="6 0 0 0 6 0 0 0 6" pbc="T T T"\nH 0 0 0\n')
    cif = sources/'ordered.cif'
    cif.write_text('data_synthetic\n_cell_length_a 6\n_cell_length_b 7\n_cell_length_c 8\n'
                   '_cell_angle_alpha 90\n_cell_angle_beta 90\n_cell_angle_gamma 90\n'
                   "_space_group_name_H-M_alt 'P -1'\n_space_group_IT_number 2\n"
                   'loop_\n_space_group_symop_id\n_space_group_symop_operation_xyz\n'
                   "1 'x,y,z'\n2 '-x,-y,-z'\n"
                   'loop_\n_atom_site_label\n_atom_site_type_symbol\n_atom_site_fract_x\n'
                   '_atom_site_fract_y\n_atom_site_fract_z\n_atom_site_occupancy\n'
                   'H1 H 0.1 0.2 0.3 1\n')
    profile = {
        'name': 'invented-smoke-profile',
        'incar': {'GGA': 'PE', 'ENCUT': 300, 'PREC': 'Normal', 'ISPIN': 1,
                  'ISMEAR': 0, 'SIGMA': 0.05, 'EDIFF': 1e-5, 'NELM': 20,
                  'ALGO': 'Normal', 'ISTART': 0, 'ICHARG': 2},
        'kpoints': {'mode': 'Gamma', 'mesh': [1, 1, 1], 'shift': [0, 0, 0]},
        'potentials': {'root': 'library', 'variants': {'H': 'H'},
                       'requirements': {'family': 'PAW_PBE'}},
    }
    specs = {}
    for mode in ('static', 'fixed-cell-relaxation'):
        specification = {'schema_version': 1, 'calculation': mode,
                         'structure': 'source.POSCAR', 'profile': profile}
        if mode == 'fixed-cell-relaxation':
            specification['overrides'] = {'incar': {'IBRION': 2, 'NSW': 3, 'EDIFFG': -0.05, 'POTIM': 0.5}}
        path = sources/(mode+'.json')
        path.write_text(json.dumps(specification))
        specs[mode] = path
    source_snapshot = {path: path.read_bytes() for path in sources.rglob('*') if path.is_file()}
    queue = root/'unexpected-jobs-state'
    child_env = dict(os.environ)
    child_env.pop('PYTHONPATH', None)
    child_env.update(PYTHONDONTWRITEBYTECODE='1', CMW_JOBS_STATE=str(queue))
    guard = '''import sys
def forbid(*args, **kwargs):
    raise AssertionError("Preparation attempted execution or Jobs mutation")
def audit(event, args):
    if event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.fork"}:
        forbid()
sys.addaudithook(audit)
from cmw.jobs.store import Store
from cmw.jobs import runtime
Store.add = forbid
runtime.start = runtime.stop = forbid
from cmw.cli import main
raise SystemExit(main(sys.argv[1:]))
'''

    def invoke(arguments, expected=0, guarded=False):
        prefix = [sys.executable, '-c', guard] if guarded else [cli]
        result = subprocess.run([*prefix, *map(str, arguments)], cwd=project, env=child_env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == expected, (arguments, result.returncode,
                                                result.stdout[-4000:], result.stderr[-4000:])
        assert not queue.exists(), 'Preparation unexpectedly created Jobs state'
        return json.loads(result.stdout)

    standalone = project/'standalone.POTCAR'
    potcar_args = ['vasp', 'potcar', 'build', structure, '--potcar-root', library,
                  '--output', standalone, '--strict-identity', '--json']
    preview = invoke([*potcar_args, '--dry-run'])
    assert not preview['written'] and not standalone.exists()
    built = invoke(potcar_args)
    assert built['written'] and built['selection']['species'] == ['H', 'H']
    checked = invoke(['vasp', 'potcar', 'check', structure, '--potcar', standalone,
                      '--strict-identity', '--json'])
    assert checked['identity_valid'] and checked['dataset_count'] == 2
    assert checked['library_release']['basis'] == 'unestablished'

    bundles = {}
    for mode, spec in specs.items():
        output, record_dir = project/mode, scratch/mode
        arguments = ['vasp', 'prepare', '--spec', spec, '--output', output,
                     '--scratch-root', scratch, '--record-directory', mode, '--json']
        planned = invoke([*arguments, '--dry-run'])
        assert planned['dry_run'] and not output.exists() and not record_dir.exists()
        invoke(arguments)
        assert {path.name for path in output.iterdir()} == {'INCAR', 'KPOINTS', 'POSCAR', 'POTCAR'}
        assert (output/'POSCAR').read_bytes() == source_snapshot[structure]
        record = record_dir/'preparation.json'
        assert record.is_file()
        assessment = invoke(['vasp', 'check-inputs', output, '--preparation-record', record, '--json'])
        assert assessment['status'] == 'valid'
        assert invoke(['vasp', 'check-inputs', output, '--json'])['status'] == 'valid'
        bundles[mode] = output
    invoke(['vasp', 'prepare', '--spec', specs['static'], '--output', project/'guarded-static',
            '--scratch-root', scratch, '--record-directory', 'guarded-static', '--json'], guarded=True)

    for case in ('invalid', 'unsupported'):
        directory = project/case
        directory.mkdir()
        for source in bundles['static'].iterdir():
            (directory/source.name).write_bytes(source.read_bytes())
        if case == 'invalid':
            (directory/'POSCAR').write_text('malformed POSCAR\n')
        else:
            (directory/'KPOINTS').write_text('invented line path\n2\nLine-mode\nReciprocal\n0 0 0\n0.5 0 0\n')
        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        assessment = invoke(['vasp', 'check-inputs', directory, '--json'], expected=1 if case == 'invalid' else 2)
        assert assessment['status'] == case
        assert {path.name: path.read_bytes() for path in directory.iterdir()} == before

    embedding_dir = project/'embedding'
    embedding_dir.mkdir()
    embedded = embedding_dir/'POSCAR'
    embed_args = ['structure', 'embed-molecule', '--input', molecule, '--output', embedded,
                  '--scratch-root', scratch, '--record-directory', 'embedding', '--json']
    invoke([*embed_args, '--dry-run'])
    assert not embedded.exists() and not (scratch/'embedding').exists()
    invoke(embed_args)
    assert {path.name for path in embedding_dir.iterdir()} == {'POSCAR'}
    embedding = json.loads((scratch/'embedding'/'preparation.json').read_text())
    assert embedding['record_kind'] == 'molecular-embedding'
    assert embedding['mapping']['poscar_to_original'] == {'0': 0, '1': 1}
    invoke(embed_args, expected=2)
    invoke(['structure', 'embed-molecule', '--input', periodic_xyz, '--output', embedding_dir/'periodic.POSCAR',
            '--scratch-root', scratch, '--record-directory', 'periodic-unsupported', '--json'], expected=2)
    assert not (embedding_dir/'periodic.POSCAR').exists()
    assert not (scratch/'periodic-unsupported').exists()

    import_dir = project/'cif-import'
    import_dir.mkdir()
    imported_poscar = import_dir/'POSCAR'
    import_args = ['structure', 'import-cif', '--input', cif, '--output', imported_poscar,
                   '--scratch-root', scratch, '--record-directory', 'cif-import',
                   '--block', 'synthetic', '--json']
    planned = invoke([*import_args, '--dry-run'])
    assert planned['dry_run'] and not imported_poscar.exists() and not (scratch/'cif-import').exists()
    imported = invoke(import_args, guarded=True)
    assert {path.name for path in import_dir.iterdir()} == {'POSCAR'}
    import_record = scratch/'cif-import'/'preparation.json'
    assert {path.name for path in import_record.parent.iterdir()} == {'preparation.json'}
    assert imported['record_kind'] == 'periodic-structure-import'
    assert imported['publication']['state'] == 'complete'
    assert imported['structure']['structure_id'] == planned['structure']['structure_id']
    assert len(imported['structure']['atoms']) == 2
    assert {item['source_site_index'] for item in imported['structure']['atoms']} == {0}
    assert {item['expanded_index'] for item in imported['structure']['atoms']} == {0, 1}
    assert imported['structure']['cell'][0] == [6.0, 0.0, 0.0]
    invoke(import_args, expected=2)

    # This specification is outside the calculation inputs and uses the
    # previously accepted profile with a separately identified import record.
    imported_spec = root/'imported-static.json'
    imported_profile = {**profile, 'potentials': {**profile['potentials'], 'root': str(library)}}
    imported_spec.write_text(json.dumps({'schema_version': 1, 'calculation': 'static',
                                        'structure': str(imported_poscar), 'profile': imported_profile,
                                        'source_record': str(import_record)}))
    imported_bundle = project/'imported-static'
    prepared = invoke(['vasp', 'prepare', '--spec', imported_spec, '--output', imported_bundle,
                       '--scratch-root', scratch, '--record-directory', 'imported-static',
                       '--json'], guarded=True)
    assert {path.name for path in imported_bundle.iterdir()} == {'INCAR', 'KPOINTS', 'POSCAR', 'POTCAR'}
    assert (imported_bundle/'POSCAR').read_bytes() == imported_poscar.read_bytes()
    assert 'periodic_structure_import' in {source['role'] for source in prepared['sources']}
    assert {path.name for path in (scratch/'imported-static').iterdir()} == {'preparation.json'}
    assert invoke(['vasp', 'check-inputs', imported_bundle, '--preparation-record',
                   scratch/'imported-static'/'preparation.json', '--json'])['status'] == 'valid'
    assert not list(project.rglob('preparation.json'))
    assert not list(project.rglob('atom_mapping.json'))
    assert not list(project.rglob('conversion_metadata.json'))
    assert {path: path.read_bytes() for path in sources.rglob('*') if path.is_file()} == source_snapshot
    assert not queue.exists()
    return {'potcar_identity_preview_build_check': 'PASS', 'input_checker': 'PASS',
            'static_and_fixed_cell': 'PASS', 'four_files_and_scratch_only_records': 'PASS',
            'molecular_embedding': 'PASS', 'invalid_and_unsupported': 'PASS',
            'ordered_cif_import_and_preparation_lineage': 'PASS',
            'sources_unchanged': 'PASS', 'execution_and_jobs_guard': 'PASS',
            'pythonpath_removed': True}


def result_surface(root, cli):
    """Exercise evidence, explicit policy and core finalization without a runner."""
    from cmw.core.artifacts import EnergyArtifact, StructureArtifact, artifact_from_dict
    from cmw.core.preparation_publication import PublicationError
    from cmw.core.provenance import stable_hash
    from cmw.periodic.vasp.result_finalization import finalize_result
    from tests.periodic.vasp.result_case import make_case, write_json

    root.mkdir()
    cases = {'static': make_case(root/'static'),
             'constrained': make_case(root/'constrained', relaxation=True, constrained=True)}
    strict = make_case(root/'strict-force', relaxation=True, constrained=True)
    strict['policy']['force'] = {'criterion': 'explicit', 'scope': 'free',
                                'threshold_ev_per_angstrom': 0.001}
    strict['spec']['policy_id'] = stable_hash(strict['policy'])
    write_json(strict['policy_path'], strict['policy'])
    write_json(strict['spec_path'], strict['spec'])
    all_cases = [*cases.values(), strict]
    protected = {path: path.read_bytes() for case in all_cases
                 for path in case['root'].rglob('*') if path.is_file()}
    queue = root/'unexpected-jobs-state'
    child_env = dict(os.environ)
    child_env.pop('PYTHONPATH', None)
    child_env.update(PYTHONDONTWRITEBYTECODE='1', CMW_JOBS_STATE=str(queue))
    guard = '''import sys
def forbid(*args, **kwargs):
    raise AssertionError("Result evidence attempted execution or mutable Jobs access")
def audit(event, args):
    if event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.fork"}:
        forbid()
sys.addaudithook(audit)
from cmw.jobs.store import Store
from cmw.jobs import runtime
Store.__init__ = Store.add = forbid
runtime.start = runtime.stop = forbid
from cmw.cli import main
raise SystemExit(main(sys.argv[1:]))
'''

    def invoke(arguments, expected=0, *, guarded=False, as_json=True):
        prefix = [sys.executable, '-c', guard] if guarded else [cli]
        result = subprocess.run([*prefix, *map(str, arguments)], cwd=root, env=child_env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == expected, (arguments, result.returncode,
                                               result.stdout[-4000:], result.stderr[-4000:])
        assert not queue.exists(), 'Result evidence unexpectedly created Jobs state'
        if not as_json:
            return result.stdout
        def invalid_constant(value):
            raise AssertionError('Nonfinite JSON literal: '+value)
        return json.loads(result.stdout, parse_constant=invalid_constant)

    for name, case in cases.items():
        diagnostic = ['vasp', 'inspect-result', case['run']]
        evidence = invoke([*diagnostic, '--json'], guarded=True)
        assert evidence['status'] == 'inspected' and evidence['policy_assessment'] is None
        assert evidence['execution']['exit_code'] is None
        assert evidence['selected_segment']['termination']['normal_footer']
        assert evidence['electronic_table_correspondence'] == 'matched'
        assert evidence['coverage']['full_electronic_history']
        human = invoke(diagnostic, as_json=False)
        assert 'Process exit: unobserved' in human and 'Native electronic convergence:' in human
        assert 'Numeric electronic convergence:' in human and 'Policy ' not in human
        assessment_args = [*diagnostic, '--policy', case['policy_path'], '--spec', case['spec_path']]
        accepted = invoke([*assessment_args, '--json'])
        assert accepted['policy_assessment']['status'] == 'PASS'
        assert accepted['binding']['valid']
        assert accepted['binding']['execution']['status'] == 'eligible'
        assert 'Policy '+case['policy']['name']+': PASS' in invoke(assessment_args, as_json=False)
        if name == 'constrained':
            forces = accepted['endpoint']['force_summary']
            assert forces['free']['count'] == forces['fixed']['count'] == 1
            assert forces['fixed']['maximum_norm'] == .5
            assert forces['free']['maximum_norm'] < .02
        finalize_args = ['vasp', 'finalize-result', case['run'], '--policy', case['policy_path'],
                         '--spec', case['spec_path'], '--scratch-root', case['scratch'],
                         '--record-directory', 'accepted', '--json']
        preview = invoke([*finalize_args, '--dry-run'])
        assert preview['publication']['state'] == 'preview'
        assert list(case['scratch'].iterdir()) == []
        record = invoke(finalize_args, guarded=True)
        assert record['publication']['state'] == 'complete'
        assert record['artifact_bundle']['status'] == 'FINALIZED'
        assert record['artifact_bundle']['artifact_count'] == 1
        artifact = artifact_from_dict(record['artifact_bundle']['artifacts'][0])
        assert artifact.validation.passed
        if name == 'static':
            assert isinstance(artifact, EnergyArtifact)
            assert artifact.metadata['energy_kind'] == 'sigma_to_zero'
            assert artifact.metadata['energy']['value'] == -2.995
        else:
            assert isinstance(artifact, StructureArtifact)
            assert artifact.charge is None and artifact.multiplicity is None
            assert artifact.metadata['periodic_geometry']['constraints'] == [[True]*3, [False]*3]
            assert artifact.metadata['periodic_geometry']['cartesian'][0][0] == .54
        record_path = case['scratch']/'accepted'/'finalization.json'
        assert {path.name for path in record_path.parent.iterdir()} == {'finalization.json'}
        assert json.loads(record_path.read_text()) == record
        checked = invoke(['vasp', 'verify-result-record', record_path, '--policy', case['policy_path'], '--json'])
        assert checked['valid'] and checked['artifacts'][0]['artifact_id'] == artifact.artifact_id
        invoke(finalize_args, expected=2)

    unknown = invoke(['vasp', 'inspect-result', cases['static']['run'], '--policy',
                      cases['static']['policy_path'], '--json'], expected=2)
    assert unknown['policy_assessment']['status'] == 'UNKNOWN'
    rejected = invoke(['vasp', 'inspect-result', strict['run'], '--policy', strict['policy_path'],
                       '--spec', strict['spec_path'], '--json'], expected=1)
    assert rejected['policy_assessment']['status'] == 'FAIL'
    refusal = invoke(['vasp', 'finalize-result', strict['run'], '--policy', strict['policy_path'],
                      '--spec', strict['spec_path'], '--scratch-root', strict['scratch'],
                      '--record-directory', 'refused', '--json'], expected=2)
    assert refusal['code'] == 'FINALIZATION_REFUSED' and not list(strict['scratch'].iterdir())

    # A revalidation failure after Scratch intent must retain a failed record,
    # never publish an accepted artifact bundle as a completed finalization.
    case = cases['static']
    with (patch('subprocess.Popen', side_effect=AssertionError('must not launch')),
            patch('cmw.jobs.store.Store', side_effect=AssertionError('must not open Jobs')),
            patch('cmw.periodic.vasp.result_finalization.revalidate_sources', return_value={'valid': False})):
        try:
            finalize_result(case['run'], policy_path=case['policy_path'], spec_path=case['spec_path'],
                            scratch_root=case['scratch'], record_directory='failed-publication')
        except PublicationError as error:
            assert error.code == 'PUBLICATION_FAILED' and not error.incomplete
        else:
            raise AssertionError('Failed source revalidation unexpectedly finalized')
    failed_path = case['scratch']/'failed-publication'/'finalization.json'
    assert json.loads(failed_path.read_text())['publication']['state'] == 'failed'
    assert not invoke(['vasp', 'verify-result-record', failed_path, '--json'], expected=2)['valid']

    for case in all_cases:
        assert {path.name for path in case['inputs'].iterdir()} == {'INCAR', 'KPOINTS', 'POSCAR', 'POTCAR'}
        assert not list(case['run'].glob('*finalization*'))
        assert not list(case['inputs'].glob('*finalization*'))
        assert {path for path in case['state'].rglob('*') if path.is_file()} <= set(protected)
    assert all(path.read_bytes() == data for path, data in protected.items())
    new_files = {path for path in root.rglob('*') if path.is_file()} - set(protected)
    assert new_files == {case['scratch']/'accepted'/'finalization.json' for case in cases.values()} | {failed_path}
    assert not queue.exists()
    return {'static_and_constrained_native_evidence': 'PASS', 'explicit_policy_pass_fail_unknown': 'PASS',
            'core_energy_and_periodic_structure_artifacts': 'PASS', 'dry_run_and_scratch_only_finalization': 'PASS',
            'record_reuse_and_no_clobber': 'PASS', 'failed_publication_semantics': 'PASS',
            'sources_four_inputs_and_saved_jobs_unchanged': 'PASS', 'execution_and_jobs_guard': 'PASS',
            'pythonpath_removed': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=('core', 'jobs', 'sdist', 'editable'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-root', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    origin = Path(cmw.__file__).resolve()
    expected = args.source_root.resolve()/'src' if args.kind == 'editable' else Path(sys.prefix).resolve()
    assert origin.is_relative_to(expected), (origin, expected)
    assert not Path.cwd().resolve().is_relative_to(args.source_root.resolve())
    assert Path(runtime.__file__).with_name('payload.sh').is_file()
    assert 'textual' not in sys.modules
    assert (importlib.util.find_spec('textual') is not None) == (args.kind != 'core')
    cli = str(Path(sys.executable).parent/'cmw')
    assert 'jobs' in command([cli, '--help'])
    assert 'Bounded Sharing' in command([cli, 'jobs', '--help'])
    missing = args.output/'missing-state'
    fixture_cli = [sys.executable, '-m', 'tests.jobs.isolated_runtime', 'cli',
                   'jobs', '--state', str(missing)]
    assert 'CMW / JOBS' in command([*fixture_cli, 'status'])
    state = json.loads(command([*fixture_cli, 'status', '--json']))
    assert not state['jobs'] and not missing.exists()
    if args.kind == 'core':
        assert 'Console dependency missing' in command([*fixture_cli, 'watch'], expected=2)
    else:
        from cmw.jobs.tui import JobsApp
        async def tui():
            with patch.object(JobsApp, 'collect_activity'):
                app = JobsApp(Store(missing))
                async with app.run_test(size=(100, 32)) as pilot:
                    await pilot.pause()
                    assert app.query_one('#table') is not None
        asyncio.run(tui())
        assert not missing.exists()
    report = {'kind': args.kind, 'python': sys.version, 'package_origin': str(origin),
              'cli_json_asset_optional_boundary': 'PASS',
              'tui': 'not installed (expected)' if args.kind == 'core' else 'PASS',
              'vasp_preparation': preparation_surface(args.output/'vasp-preparation', cli),
              'vasp_result_evidence': result_surface(args.output/'vasp-results', cli),
              'lifecycle': [lifecycle(args.output/mode, mode)
                            for mode in ('sequential', 'bounded-sharing')]}
    (args.output/'result.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
