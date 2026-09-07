from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from cmw.core.execution_profiles import ExecutionProfile, OrcaResourcePolicy, MultiwfnResourcePolicy
from cmw.core.plan_materialization import WorkflowPlanMaterializer
from cmw.core.structure_artifacts import structure_artifact_from_file
from cmw.molecular.orca.hfld import HFLDError, parse_hfld, parse_rhf_reference, assemble_hfld
from cmw.molecular.orca.renderer import OrcaExecutionRenderer
from cmw.molecular.stacking.hole_electron import FragmentDefinition
from cmw.molecular.workflows.hfld_led import HFLDFragmentState, build_hfld_plan, hfld_spec

FIXTURE = Path(__file__).parents[2] / 'fixtures/orca/hfld/water_dimer_6_1_1_excerpt.out'


class NativeHFLDTests(unittest.TestCase):
    def test_native_energy_not_intermediate_ccsd(self):
        result = parse_hfld(FIXTURE.read_text())
        self.assertAlmostEqual(result.total, -152.112956231503)
        self.assertAlmostEqual(result.dispersion, -0.001056073)
        self.assertLess(abs(result.singles), 1e-6)
        values = assemble_hfld(result, (-76.05, -76.05))
        self.assertLess(abs(values['closure_residual']), 1e-8)
        self.assertNotIn('triples', values)
        self.assertNotIn('non_dispersive_ccsd', values)
        self.assertNotEqual(values['hf_led_numerical_difference'], 0)

    def test_fail_closed_native_evidence(self):
        text = FIXTURE.read_text()
        for modified in (text.replace('Inter-fragment dispersion', 'missing dispersion'),
                         text.replace('-152.112956231503', '-152.113285632051'),
                         text.replace('Singles energy                          0.000000004', 'Singles energy                          0.000010004'),
                         text.replace('Intra fragment   2 (REF.)', 'Intra fragment   3 (REF.)'),
                         text + '\n****ORCA TERMINATED NORMALLY****',
                         text.replace('Intrafragment pairs 2 <-> 2 neglected for CCSD', 'missing')):
            with self.subTest(modified=modified[-100:]), self.assertRaises(HFLDError):
                parse_hfld(modified)

    def test_rhf_reference_and_closure(self):
        text = '| 1> ! RHF def2-TZVP(-f) VeryTightSCF\nTotal Energy : -76.050000000 Eh\nFINAL SINGLE POINT ENERGY -76.050000000\nORCA TERMINATED NORMALLY\n'
        self.assertEqual(parse_rhf_reference(text), -76.05)
        with self.assertRaises(HFLDError):
            parse_rhf_reference(text.replace('! RHF', '! HFLD'))
        with self.assertRaises(HFLDError):
            assemble_hfld(parse_hfld(FIXTURE.read_text()), [float('nan'), -76.05])

    def test_plan_render_identity_partition_and_own_basis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            xyz = root/'water.xyz'
            xyz.write_text('6\nwater pair\nO 0 0 0\nH .758 0 .504\nH -.758 0 .504\nO 0 0 2.9\nH .758 0 3.404\nH -.758 0 3.404\n')
            structure = structure_artifact_from_file(xyz, source='synthetic', charge=0, multiplicity=1)
            fragments = (FragmentDefinition('A', (0,1,2)), FragmentDefinition('B', (3,4,5)))
            states = (HFLDFragmentState('A',0,1), HFLDFragmentState('B',0,1))
            plan = build_hfld_plan(structure, fragments, states, hfld_spec())
            profile = ExecutionProfile('local', OrcaResourcePolicy(8, 30.0), MultiwfnResourcePolicy(1,1.0))
            materializer = WorkflowPlanMaterializer({'orca': OrcaExecutionRenderer()})
            materialized = [materializer.materialize_node(plan.execution_plan, node.node_id,
                project_root=root/'canonical', system_identifier='water', resource_profile=profile,
                runtime_identity={'orca': {'path':'/synthetic/orca','version':'6.1.1'}}) for node in plan.execution_plan.nodes]
            dimer, a, b = materialized
            inp = Path(dimer.to_dict()['renderer_metadata']['launch_contract']['input']).read_text()
            self.assertIn('HFLD def2-TZVP(-f)', inp)
            self.assertIn('DoLEDHF true', inp)
            self.assertIn('TCutPNO 3.33e-7', inp)
            self.assertIn('TCutPairs 1e-5', inp)
            self.assertIn('O(1)', (dimer.layout.working_directory/'input.xyz').read_text())
            self.assertIn('O(2)', (dimer.layout.working_directory/'input.xyz').read_text())
            for reference in (a,b):
                text = (reference.layout.working_directory/'stage.inp').read_text()
                self.assertIn('RHF', text)
                self.assertNotIn('%mdci', text)
                self.assertNotIn('HFLD', text)
                self.assertEqual((reference.layout.working_directory/'input.xyz').read_text().splitlines()[0], '3')
                self.assertNotIn(':', (reference.layout.working_directory/'input.xyz').read_text())
            self.assertEqual(len({m.target["target_id"] for m in materialized}), 3)
            same = materializer.materialize_node(plan.execution_plan,'led_dimer',project_root=root/'other-operational-path',system_identifier='water',resource_profile=profile,runtime_identity={'orca': {'path':'/synthetic/orca','version':'6.1.1'}})
            self.assertEqual(dimer.target["target_id"], same.target["target_id"])
            self.assertEqual(dimer.attempt.attempt_id, 'attempt_001')
            with self.assertRaises(HFLDError):
                build_hfld_plan(structure,(fragments[0],FragmentDefinition('B',(2,3,4,5))),states,hfld_spec())
            with self.assertRaises(HFLDError):
                build_hfld_plan(structure,fragments,states,replace(hfld_spec(),keywords=hfld_spec().keywords.replace('HFLD', 'DLPNO-CCSD(T) LED')))
            self.assertNotIn('assemble_interfragment_led', Path(__import__('cmw.molecular.workflows.hfld_led',fromlist=['']).__file__).read_text())
