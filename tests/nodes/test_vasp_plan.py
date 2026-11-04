"""Synthetic parsed input contracts; no files, potentials, processes or network."""
import copy
import unittest

from cmw.nodes.protocol import NodeError
from cmw.nodes.vasp_plan import CoreTier, Hardware, plan_vasp
from cmw.periodic.vasp.inputs import parse_incar


def hardware():
    return Hardware(16, (CoreTier(0, 'Vendor tier zero', 10), CoreTier(1, 'Vendor tier one', 6)), None)


class HardwareContractTests(unittest.TestCase):
    def test_existing_strict_json_codec_roundtrip_preserves_native_names(self):
        value = Hardware(16, (CoreTier(0, ' Native 名称 / Uninterpreted ', 10),), 12)
        self.assertEqual(Hardware.from_json(value.to_json()), value)
        self.assertEqual(value.to_dict()['core_tiers'][0]['name'], ' Native 名称 / Uninterpreted ')
        self.assertEqual(Hardware.from_dict(hardware().to_dict()), hardware())

    def test_empty_tiers_and_null_policy_are_explicit(self):
        value = Hardware(16, (), None)
        self.assertEqual(Hardware.from_json(value.to_json()), value)

    def test_malformed_and_unknown_fields_rejected(self):
        original = hardware().to_dict()
        variants = [{**original, 'extra': 1}, {'physical_cores': 16},
                    {**original, 'recommended_max_parallelism': False},
                    {**original, 'physical_cores': 1.5}, {**original, 'core_tiers': {}},
                    {**original, 'core_tiers': [{'level': 0, 'name': 'x', 'physical_cores': 1, 'unknown': 1}]}]
        for value in variants:
            with self.subTest(value=value), self.assertRaises(NodeError):
                Hardware.from_dict(value)
        for raw in (b'{"physical_cores":1,"physical_cores":2}\n', b'{"x":NaN}\n', b'{}'):
            with self.subTest(raw=raw), self.assertRaises(NodeError):
                Hardware.from_json(raw)


class VaspPlanContractTests(unittest.TestCase):
    def plan(self, text='', **changes):
        options = dict(poscar_species=['Zn', 'S', 'Zn'], potcar_species=['Zn', 'S', 'Zn'],
                       mpi_ranks=16, threads_per_rank=1, hardware=hardware())
        options.update(changes)
        return plan_vasp(None if text is None else parse_incar(text), **options)

    def test_absent_tags_default_only_after_successful_parsing(self):
        plan = self.plan()
        self.assertTrue(plan.complete)
        self.assertTrue(plan.permitted)
        self.assertEqual((plan.effective_kpar, plan.split_tag, plan.split_value), (1, 'NCORE', 1))
        self.assertIsNone(plan.require_submission())
        missing = self.plan(None)
        self.assertFalse(missing.complete)
        self.assertIsNone(missing.effective_kpar)
        with self.assertRaises(NodeError) as caught:
            missing.require_submission()
        self.assertEqual(caught.exception.code, 'INVALID_SUBMISSION')

    def test_npar_precedence_and_effective_split_warning(self):
        for ncore in ('3', '0', '-1'):
            with self.subTest(ncore=ncore):
                plan = self.plan(f'KPAR=2; NPAR=4; NCORE={ncore}')
                self.assertTrue(plan.permitted)
                self.assertFalse(plan.findings)
                self.assertEqual((plan.split_tag, plan.split_value), ('NPAR', 4))
        for text, code in (('KPAR=2; NPAR=3; NCORE=2', 'NPAR_INCOMPATIBLE'),
                           ('KPAR=2; NCORE=3', 'NCORE_INCOMPATIBLE')):
            plan = self.plan(text)
            self.assertTrue(plan.permitted)
            self.assertEqual([(item.code, item.severity) for item in plan.findings], [(code, 'WARNING')])
            plan.require_submission()

    def test_kpar_must_divide_ranks(self):
        for value in (3, 32):
            plan = self.plan(f'KPAR={value}')
            self.assertFalse(plan.permitted)
            self.assertIn('KPAR_INCOMPATIBLE', [item.code for item in plan.findings])
            with self.assertRaises(NodeError):
                plan.require_submission()

    def test_invalid_controlling_values_never_default(self):
        for tag in ('KPAR', 'NCORE', 'NPAR'):
            for value in ('0', '-1', 'False', '1.5'):
                with self.subTest(tag=tag, value=value):
                    self.assertFalse(self.plan(f'{tag}={value}').permitted)
        parsed = parse_incar('KPAR=2')
        parsed['settings']['NPAR'] = None
        plan = plan_vasp(parsed, poscar_species=['H'], potcar_species=['H'], mpi_ranks=16,
                         threads_per_rank=1, hardware=hardware())
        self.assertIn('INVALID_NPAR', [item.code for item in plan.findings])

    def test_invalid_and_unsupported_parse_results_are_incomplete(self):
        for text in ('KPAR=2; KPAR=4', 'KPAR = bad', 'KPAR=2 \\'):
            with self.subTest(text=text):
                plan = self.plan(text)
                self.assertFalse(plan.complete)
                self.assertFalse(plan.permitted)
                self.assertIn('INCAR', plan.unassessed)
                self.assertIsNone(plan.effective_kpar)

    def test_physical_core_product_boundary(self):
        self.assertTrue(self.plan(mpi_ranks=8, threads_per_rank=2).permitted)
        plan = self.plan(mpi_ranks=9, threads_per_rank=2)
        self.assertFalse(plan.permitted)
        self.assertIn('PHYSICAL_CORES_EXCEEDED', [item.code for item in plan.findings])

    def test_policy_boundary_and_null_policy(self):
        for limit, warning in ((16, False), (15, True), (None, False)):
            with self.subTest(limit=limit):
                plan = self.plan(hardware=Hardware(16, (), limit))
                self.assertTrue(plan.permitted)
                self.assertEqual(any(item.code == 'RECOMMENDED_PARALLELISM_EXCEEDED' for item in plan.findings), warning)
        plan = self.plan(hardware=Hardware(16, (), 8), mpi_ranks=17)
        self.assertEqual({item.severity for item in plan.findings}, {'ERROR', 'WARNING'})

    def test_native_tier_names_do_not_create_policy(self):
        baseline = self.plan()
        arbitrary = Hardware(16, (CoreTier(0, 'Efficiency / performance / arbitrary tier', 1),), None)
        self.assertEqual(self.plan(hardware=arbitrary), baseline)

    def test_species_order_and_repeated_blocks(self):
        self.assertTrue(self.plan().permitted)
        self.assertFalse(self.plan(potcar_species=['Zn', 'Zn', 'S']).permitted)
        for key in ('potcar_species', 'poscar_species'):
            with self.subTest(key=key):
                plan = self.plan(**{key: None})
                self.assertFalse(plan.complete)
                with self.assertRaises(NodeError):
                    plan.require_submission()

    def test_missing_and_invalid_resources_block_submission(self):
        for key in ('mpi_ranks', 'threads_per_rank'):
            for value in (None, True, 0, -1, 1.5):
                with self.subTest(key=key, value=value):
                    plan = self.plan(**{key: value})
                    self.assertFalse(plan.permitted)
                    with self.assertRaises(NodeError):
                        plan.require_submission()
        self.assertFalse(self.plan(hardware=None).complete)

    def test_pure_plan_preserves_input_parser_and_hardware_records(self):
        parsed = parse_incar('KPAR=2; NCORE=4')
        target = hardware().to_dict()
        before = copy.deepcopy((parsed, target))
        plan_vasp(parsed, poscar_species=['H'], potcar_species=['H'], mpi_ranks=16,
                  threads_per_rank=1, hardware=target)
        self.assertEqual((parsed, target), before)

    def test_malformed_parser_record_is_not_absent_tags(self):
        for parsed in ({}, {'settings': {}, 'status': 'valid'},
                       {'settings': {'kpar': 2}, 'findings': [], 'status': 'valid'}):
            with self.subTest(parsed=parsed), self.assertRaises(NodeError):
                plan_vasp(parsed, poscar_species=['H'], potcar_species=['H'], mpi_ranks=16,
                          threads_per_rank=1, hardware=hardware())


if __name__ == '__main__':
    unittest.main()
