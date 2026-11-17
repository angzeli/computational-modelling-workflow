"""SCF classifier (#41 Phase 2a) against the Phase 0 parser-contract fixtures."""
import json
from pathlib import Path
import tempfile
import unittest

from cmw.periodic.vasp.scf_classifier import classify, classify_run

FIXTURES = Path(__file__).resolve().parents[2] / 'fixtures' / 'manifests' / 'parser-contracts'
FIELDS = ('scf_result', 'last_step', 'ionic_step', 'energy_ev', 'delta_energy_ev', 'total_magnetisation_mu_b')
CONVERGED = json.loads((FIXTURES / 'converged-at-nelm.json').read_text())


class ParserContractTests(unittest.TestCase):
    def test_every_contract_fixture(self):
        paths = sorted(FIXTURES.glob('*.json'))
        self.assertEqual(len(paths), 19)
        for path in paths:
            contract = json.loads(path.read_text())
            with self.subTest(contract=path.stem):
                result = classify(contract['OUTCAR_lines'], contract['stdout_lines'],
                                  invocation_bound=contract['invocation_bound'])
                expected = contract['expected']
                self.assertEqual({key: result[key] for key in FIELDS}, {key: expected[key] for key in FIELDS})
                for item in result['evidence']:
                    self.assertEqual(set(item), {'source', 'marker', 'line'})


class ClassifierRuleTests(unittest.TestCase):
    def outcar(self, *extra, base=CONVERGED['OUTCAR_lines']):
        return [*base, *extra]

    def test_file_entry_reads_outcar_and_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()  # The snapshot reader refuses symlinked paths.
            outcar, stdout = root / 'OUTCAR', root / 'stdout'
            outcar.write_text('\n'.join(CONVERGED['OUTCAR_lines']) + '\n')
            stdout.write_text('\n'.join(CONVERGED['stdout_lines']) + '\n')
            self.assertEqual(classify_run(outcar, stdout, invocation_bound=True)['scf_result'], 'converged')
            self.assertEqual(classify_run(outcar, invocation_bound=True)['last_step'], None)

    def test_unmatched_stdout_is_ignored_and_never_supplies_metrics_or_stops(self):
        stdout = [CONVERGED['stdout_lines'][0].replace('-1.0000000E+01', '-9.0000000E+00', 1),
                  CONVERGED['stdout_lines'][1], 'hard stop encountered! aborting job ...']
        result = classify(CONVERGED['OUTCAR_lines'], stdout, invocation_bound=True)
        self.assertEqual((result['scf_result'], result['last_step']), ('converged', None))
        self.assertIn('do not match', result['evidence'][0]['marker'])

    def test_multiple_invocations_and_echoed_markers(self):
        twice = classify(self.outcar('vasp.6.6.1'), CONVERGED['stdout_lines'], invocation_bound=True)
        self.assertEqual(twice['scf_result'], 'unknown')
        echoed = ['vasp.6.6.1', ' INCAR:', '   aborting loop because hard stop was set', ' POTCAR: fake',
                  'Iteration 1( 1)']
        self.assertEqual(classify(echoed, invocation_bound=True)['scf_result'], 'unknown')

    def test_opposing_markers_and_numeric_contradiction_are_unknown(self):
        opposing = classify(self.outcar('EDIFF was not reached'), CONVERGED['stdout_lines'], invocation_bound=True)
        self.assertEqual(opposing['scf_result'], 'unknown')
        stdout = [CONVERGED['stdout_lines'][0],
                  'DAV: 2 -1.000000001E+01 -1.0E-02 -1.0E-08 16 1.0E-08']
        self.assertEqual(classify(CONVERGED['OUTCAR_lines'], stdout, invocation_bound=True)['scf_result'], 'unknown')

    def test_vector_magnetisation_and_augmentation_stay_null(self):
        outcar = [line.replace('magnetization 2.0000000', 'magnetization 1.0 0.0 0.0')
                  for line in CONVERGED['OUTCAR_lines']]
        self.assertIsNone(classify(outcar, CONVERGED['stdout_lines'], invocation_bound=True)['total_magnetisation_mu_b'])
        outcar = [line.replace('number of electron', 'augmentation part number of electron')
                  for line in CONVERGED['OUTCAR_lines']]
        self.assertIsNone(classify(outcar, CONVERGED['stdout_lines'], invocation_bound=True)['total_magnetisation_mu_b'])

    def test_native_dialect_whitespace_is_accepted(self):
        outcar = ['vasp.6.6.1', ' Startparameter for this run:', '   NELM   =      2;   NELMIN=  2; NELMDL= -5',
                  '   EDIFF  = 0.1E-05   stopping-criterion for ELM',
                  '--------------------------------------- Iteration      1(   1)  ---------------------------------------',
                  '  free energy    TOTEN  =       -10.00000000 eV',
                  '--------------------------------------- Iteration      1(   2)  ---------------------------------------',
                  '  free energy    TOTEN  =       -10.00000001 eV',
                  ' number of electron       8.0000000 magnetization       2.0000000',
                  '------------------------ aborting loop because EDIFF is reached ----------------------------------------']
        stdout = ['DAV:   1    -0.100000000000E+02   -0.10000E+02   -0.10000E+01    16   0.100E-02',
                  'CG :   2    -0.100000000100E+02   -0.10000E-07   -0.10000E-07    16   0.100E-07']
        result = classify(outcar, stdout, invocation_bound=True)
        self.assertEqual((result['scf_result'], result['last_step'], result['total_magnetisation_mu_b']),
                         ('converged', 2, 2.0))


if __name__ == '__main__':
    unittest.main()
