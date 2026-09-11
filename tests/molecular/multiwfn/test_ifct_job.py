"""Synthetic end-to-end tests; never invoke a scientific engine."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from cmw.molecular.multiwfn.ifct_job import finalize
from cmw.molecular.orca.excited_states import parse_orca_tda_excited_states_file

ROOT = Path(__file__).resolve().parents[3]
ORCA = ROOT / 'tests/fixtures/orca/excited_states/orca_6_1_1_tda_singlets.out'

class IfctJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw-ifct-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        state = parse_orca_tda_excited_states_file(ORCA).state('singlet', 1)
        self.fake = self.root / 'Multiwfn'
        self.fake.write_text('''#!/usr/bin/env python3
import sys
menu=sys.stdin.read()
print('Version 2026.7.15')
if menu.strip() == 'q': sys.exit(0)
assert menu.splitlines() == ['18','8','2','cmw-orca-excited-state.out','1','2','1-2,4','3,5','0','0','q']
print('( Number of parallel threads: 4 Current date: synthetic )')
print('Loaded '+sys.argv[1]+' successfully!')
print(''' + repr(f'''State: 1 Exc. Energy: {state.excitation_energy_ev:.3f} eV Multi.: 1 MO pairs: 4
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
Apparent local excitation percentage, LE(%): 94.960 %''') + ')\n')
        self.fake.chmod(0o755)
        (self.root/'settings.ini').write_text('nthreads= 4\n')
        wave = self.root/'wave.molden.input';wave.write_text('[Molden Format]\n')
        geom = self.root/'geom.xyz';geom.write_text('5\nsynthetic\n')
        self.spec = self.root/'spec.json'
        self.spec.write_text(json.dumps({'orca_output':str(ORCA),'wavefunction':str(wave),
            'geometry':str(geom),'spin_manifold':'singlet','state_index':1,'atom_count':5,
            'fragments':[{'label':'A','indices_zero_based':[0,1,3]},
                         {'label':'B','indices_zero_based':[2,4]}]}))
        self.attempt=self.root/'attempt_001'
        self.env=dict(os.environ, PYTHON_BIN=os.sys.executable, MULTIWFN_EXE=str(self.fake),
                      MULTIWFN_NTHREADS='4', PYTHONDONTWRITEBYTECODE='1')

    def run_payload(self):
        return subprocess.run(['bash',str(ROOT/'scripts/multiwfn/run_ifct.sh'),
            str(self.spec),str(self.attempt)],env=self.env,text=True,capture_output=True)

    def test_success_and_refuse_overwrite(self):
        run=self.run_payload();self.assertEqual(run.returncode,0,run.stderr)
        result=json.loads((self.attempt/'result.json').read_text())
        self.assertEqual(result['validation_status'],'PASSED')
        self.assertEqual(result['parsed']['transfer']['net_source_to_target_electrons'],0.0504)
        self.assertNotEqual(self.run_payload().returncode,0)
        with self.assertRaisesRegex(ValueError,'exited'):
            finalize(self.attempt,1)
        (self.attempt/'menu.in').write_text('q\n')
        with self.assertRaisesRegex(ValueError,'menu changed'):
            finalize(self.attempt,0)

    def test_partition_rejected_before_launch(self):
        spec=json.loads(self.spec.read_text());spec['fragments'][1]['indices_zero_based']=[0,2,4]
        self.spec.write_text(json.dumps(spec));run=self.run_payload()
        self.assertNotEqual(run.returncode,0)
        self.assertFalse((self.attempt/'multiwfn.session.log').exists())

if __name__=='__main__':unittest.main()
