"""Public synthetic source and harmless executables for first-class Jobs tests."""
import json
from pathlib import Path
import shutil
import uuid

from cmw.core.job import JobTarget
from cmw.core.provenance import ArtifactRecord, file_hash
from cmw.molecular.orca.excited_states import parse_orca_tda_excited_states_file
from cmw.structure.xyz import geometry_hash, read_xyz

ROOT = Path(__file__).resolve().parents[3]


def make_spec(root: Path, profile='FMO'):
    root.mkdir(parents=True, exist_ok=True)
    donor = root / 'validated donor'
    donor.mkdir()
    geometry = donor / 'geometry.xyz'
    geometry.write_text('5\nSynthetic public geometry\nC 0 0 0\nH 1 0 0\nH 0 1 0\nH 0 0 1\nH -1 0 0\n')
    wave = donor / 'source.molden.input'
    wave.write_text('[Molden Format]\nSynthetic source fixture; no scientific engine execution\n')
    orca = donor / 'source.out'
    shutil.copyfile(ROOT / 'tests/fixtures/orca/excited_states/orca_6_1_1_tda_singlets.out', orca)
    target = JobTarget('SP', geometry_hash(read_xyz(geometry)), 0, 1, {'keywords': 'synthetic'})
    source = donor / 'result.json'
    source.write_text(json.dumps({'schema_version': 1, 'target': target.to_dict(),
        'attempt': {'attempt_id': 'attempt_001', 'target_id': target.target_id},
        'lineage': {'source': 'synthetic public fixture', 'geometry_sha256': target.geometry_sha256,
                    'parent_stage': None, 'parent_target_id': None, 'parent_artifact_sha256': None},
        'execution': {'status': 'SUCCESS'}, 'scientific': {'status': 'VALID'}, 'reusable': True,
        'artifacts': {role: ArtifactRecord.from_path(path, role=role, relative_to=donor).__dict__
                      for role, path in {'wavefunction': wave, 'geometry': geometry, 'output': orca}.items()},
        'wavefunction_semantics': {'spin_mode': 'restricted', 'format': 'molden',
                                  'homo_index': 5, 'lumo_index': 6, 'orbital_indexing': 'one_based'}}))
    settings = root / 'settings.ini'; settings.write_text('nthreads= 8\n')
    executable = root / 'Multiwfn'
    if profile == 'IFCT':
        state = parse_orca_tda_excited_states_file(orca).state('singlet', 1)
        output = f'''State: 1 Exc. Energy: {state.excitation_energy_ev:.3f} eV Multi.: 1 MO pairs: 4
Loading configuration coefficients of excited state 1...
Radial grids: 75 Angular grids: 434 Total: 32550
Contribution of each fragment to hole and electron:
1 Hole: 60.12 % Electron: 55.08 %
2 Hole: 39.88 % Electron: 44.92 %
Construction of interfragment charger-transfer matrix has finished!
Variation of population number of fragment 1: -0.05040
Variation of population number of fragment 2: 0.05040
Intrafragment electron redistribution of fragment 1: 0.33114
Intrafragment electron redistribution of fragment 2: 0.17913
1 -> 2: 0.27440 1 <- 2: 0.22400 Net 1 -> 2: 0.05040
Intrinsic charge transfer percentage, CT(%): 49.840 %
Intrinsic local excitation percentage, LE(%): 50.160 %
Apparent charge transfer percentage, CT(%): 5.040 %
Apparent local excitation percentage, LE(%): 94.960 %'''
        executable.write_text('#!/usr/bin/env python3\nimport os,sys,time\nmenu=sys.stdin.read()\n'
            "assert menu.splitlines()==['18','8','2','cmw-orca-excited-state.out','1','2','1-2,4','3,5','0','0','q']\n"
            "print('Version 2026.7.15',flush=True)\nprint('( Number of parallel threads: 2 Current date: synthetic )',flush=True)\n"
            "print('Loaded '+sys.argv[1]+' successfully!',flush=True)\ntime.sleep(float(os.environ.get('CMW_TEST_PAUSE','0.5')))\n"
            f'print({output!r},flush=True)\n')
    else:
        shutil.copyfile(ROOT / 'tests/fixtures/fake_multiwfn.py', executable)
    executable.chmod(0o755)
    science = {'profile': profile, 'source_result': str(source)}
    if profile in {'FMO', 'ESP'}:
        science['grid_spacing_bohr'] = 0.2
    elif profile == 'IGMH':
        fragments = root / 'fragments.json'; fragments.write_text(json.dumps({'schema_version': 1, 'indexing': 'one_based', 'allow_overlap': False, 'require_complete_partition': True, 'fragments': {'A': [1, 2, 4], 'B': [3, 5]}}))
        configuration = root / 'configuration.json'; configuration.write_text(json.dumps({'schema_version': 1, 'profile': 'interfragment', 'grid_spacing_bohr': 0.2, 'cube_generation': True}))
        science.update(fragments_file=str(fragments), configuration_file=str(configuration))
    else:
        science.update(orca_output=str(orca), spin_manifold='singlet', state_index=1, atom_count=5,
                       fragments=[{'label': 'A', 'indices_zero_based': [0, 1, 3]}, {'label': 'B', 'indices_zero_based': [2, 4]}])
    return {'schema': 1, 'engine': 'Multiwfn', 'name': f'Synthetic {profile}', 'request_id': str(uuid.uuid4()),
            'intent': 'held', 'resources': {'cpus': 2, 'memory_gib': 1.0, 'mpi_ranks': 1, 'threads_per_rank': 2},
            'workdir': str(root / 'jobs work Ω'), 'runtime': {'qualification': 'synthetic', 'executable': str(executable),
              'sha256': file_hash(executable), 'version': '2026.7.15' if profile == 'IFCT' else '3.8',
              'settings_source': str(settings), 'settings_sha256': file_hash(settings), 'threads': 2,
              'menu_contract': 'multiwfn-3.8-compatible-v1'}, 'scientific': science}
