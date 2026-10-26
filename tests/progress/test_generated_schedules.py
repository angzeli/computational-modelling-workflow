"""Seeded legal delivery schedules checked against handwritten scientific facts.

Only invented bytes and disposable files are used. Expected values never come
from the batch/live parser, formatter, or a snapshot produced by another run.
"""
from pathlib import Path
import random
import tempfile
import unittest

from cmw.nodes.codec import decode_typed, encode_typed
from cmw.progress.formatting import progress_text
from cmw.progress.models import ProgressSnapshot, RunContext, SourceDescriptor
from cmw.progress.reader import RegularFileReader
from cmw.progress.service import ObservationService


# This ordinary static dialect needs no research inputs or potential payloads.
HEADER = ''' vasp.6.6.1 invented schedule fixture
 EDIFF = 0.000001
 EDIFFG = -0.02
 NELM = 8
 NSW = 0
 IBRION = -1
 ISIF = 2
 ISPIN = 1
 ICHARG = 2
 IALGO = 38
 LHFCALC = F
 LSORBIT = F
 LNONCOLLINEAR = F
 NIONS = 2
'''
NATIVE_ONE = ''' Iteration 1(1)
 EDDAV: synthetic
 free energy    TOTEN = -10.00000000 eV
'''
NATIVE_TWO_HEADER = ' Iteration 1(2)\n'
NATIVE_TWO_BODY = ''' EDDAV: synthetic
 free energy    TOTEN = -10.25000000 eV
'''
ROW_ONE = 'DAV: 1 -10.00000000 -1.00000000 -0.50000000 12 0.50000000 0.75000000\n'
ROW_TWO = 'DAV: 2 -10.25000000 -0.25000000 -0.12500000 12 0.25000000\n'
POSCAR = '''Invented whole-atom mask
1
4 0 0
0 4 0
0 0 4
H He
1 1
Selective dynamics
Cartesian
0 0 0 T T T
1 1 1 F F F
'''
FORCE_OPEN = ''' POSITION                                       TOTAL-FORCE (eV/Angst)
 -----------------------------------------------------------------------------------
0 0 0 0.300000 0.400000 0.000000
1 1 1 0.000000 0.000000 2.000000
'''
FORCE_CLOSE = ' -----------------------------------------------------------------------------------\n'


class GeneratedProgressScheduleTests(unittest.TestCase):
    def make_case(self, budget=65536, *, constraints=False):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-generated-schedule-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        sources = [SourceDescriptor('outcar', str(root / 'OUTCAR')),
                   SourceDescriptor('stdout', str(root / 'stdout.log'))]
        if constraints:
            (root / 'POSCAR').write_text(POSCAR)
            sources.append(SourceDescriptor('poscar', str(root / 'POSCAR'), optional=True))
        context = RunContext('invented-schedule', 'VASP', tuple(sources),
                             attempt_id='invented-attempt', stage='SP')
        service = ObservationService(clock=lambda: 100., monotonic_clock=lambda: 50.,
            reader_factory=lambda source: RegularFileReader(source, byte_budget=budget))
        service.clock_id = 'invented-observer-clock'
        self.addCleanup(service.close)
        return root, context, service

    def poll(self, context, service):
        snapshot = service.poll(context)
        self.assertNotIn(snapshot.status, {'READ_ERROR', 'ADAPTER_ERROR'}, snapshot.messages)
        # Strict transport preservation is checked at every intermediate state.
        remote = decode_typed(encode_typed(snapshot), ProgressSnapshot)
        self.assertEqual(remote, snapshot)
        self.assertEqual(progress_text(remote, width=100, now=100., max_lines=8, framed=True),
                         progress_text(snapshot, width=100, now=100., max_lines=8, framed=True))
        return snapshot

    def drain(self, context, service):
        for _ in range(200):
            snapshot = self.poll(context, service)
            if snapshot.completeness != 'CATCHING_UP':
                return snapshot
        self.fail('The tiny fixture exceeded its bounded read allowance')

    @staticmethod
    def append(root, role, text):
        path = root / {'outcar': 'OUTCAR', 'stdout': 'stdout.log'}[role]
        with path.open('a') as stream:
            stream.write(text)

    @staticmethod
    def metrics(snapshot):
        return {series.latest.semantic_id: series for series in snapshot.metrics}

    def assert_frame(self, snapshot):
        text = progress_text(snapshot, width=140, now=100., max_lines=8, framed=True)
        self.assertTrue(text.startswith('┌'), text)
        for label in ('SCIENTIFIC', 'SCF', 'FORCE', 'ELECTRONIC'):
            self.assertIn(label, text)
        self.assertNotIn('Scientific: BOUND', text)
        self.assertNotIn('Configure Sources', text)
        return text

    def assert_facts(self, snapshot, native_step, stream_step):
        """Milestone counts are producer facts, not inferred from parsed output.

        Native steps: settings; completed SCF1; open SCF2; completed SCF2.
        Source steps: unterminated row1; complete row1; complete row2.
        An open native header advances native context but not correspondence.
        """
        source_rows = max(0, stream_step - 1)
        native_rows = (0, 0, 1, 1, 2)[native_step]
        native_header = (None, None, 1, 2, 2)[native_step]
        metrics = self.metrics(snapshot)
        text = self.assert_frame(snapshot)
        self.assertEqual(snapshot.scientific_validation, 'not-assessed')
        if native_step:
            self.assertEqual(snapshot.calculation, 'static')
            self.assertIn('Target N/A · static', text)
            self.assertIn('EDIFF 1E-6 eV', text)
        else:
            self.assertIsNone(snapshot.calculation)
            self.assertNotIn('Target N/A · static', text)
        if not source_rows:
            self.assertFalse(metrics)
            return
        expected = ({'free_energy': -10., 'dE': 1., 'd_eps': .5, 'rms': .5, 'rms_c': .75}
                    if source_rows == 1 else
                    {'free_energy': -10.25, 'dE': .25, 'd_eps': .125, 'rms': .25, 'rms_c': None})
        self.assertEqual(set(metrics), {'vasp.electronic.' + key for key in expected})
        checked = min(source_rows, native_rows) or None
        qualification = 'row-correspondence' if checked == source_rows else 'source-local'
        for key, value in expected.items():
            metric = metrics['vasp.electronic.' + key].latest
            self.assertEqual(metric.value, value, key)
            self.assertEqual(metric.qualification, qualification, key)
            self.assertEqual(metric.context[-1].index, source_rows, key)
            self.assertEqual(metric.unit, 'native' if key in {'rms', 'rms_c'} else 'eV')
            if key in {'dE', 'd_eps'}:
                self.assertEqual(metric.signed_value, -value)
                if checked == source_rows:
                    self.assertEqual(metric.criterion.value, .000001)
                    self.assertEqual(metric.relation, 'AT_OR_ABOVE')
        check = next(item for item in snapshot.verification if item.domain == 'electronic-row')
        self.assertEqual((check.observed_frontier, check.checked_frontier, check.native_frontier),
                         (source_rows, checked, native_header))
        if native_header is not None:
            self.assertEqual([(level.kind, level.index) for level in snapshot.iteration],
                             [('evaluation', 1), ('electronic', native_header)])
            self.assertIn(f'OUTCAR SCF {native_header}/8', text)
        if checked:
            self.assertEqual(set(check.fields), {'label', 'energy', 'algorithm'})
            self.assertNotIn('rms', check.fields)
            self.assertNotIn('dE', check.fields)

    def test_seeded_legal_chunk_delivery_schedules_have_independent_oracles(self):
        # Three schedules times three reader budgets; each stream keeps byte order.
        for seed in (3, 17, 61):
            for budget in (17, 127, 65536):
                with self.subTest(seed=seed, budget=budget):
                    root, context, service = self.make_case(budget)
                    rng = random.Random(seed)
                    pieces = {'outcar': [HEADER, NATIVE_ONE, NATIVE_TWO_HEADER, NATIVE_TWO_BODY],
                              'stdout': [ROW_ONE[:-1], '\n', ROW_TWO]}
                    delivered = {'outcar': 0, 'stdout': 0}
                    self.assert_facts(self.drain(context, service), 0, 0)
                    while any(pieces.values()):
                        role = rng.choice([key for key, values in pieces.items() if values])
                        piece = pieces[role].pop(0)
                        while piece:
                            cut = rng.randint(1, min(53, len(piece)))
                            self.append(root, role, piece[:cut])
                            piece = piece[cut:]
                            self.poll(context, service)
                        delivered[role] += 1
                        snapshot = self.drain(context, service)
                        self.assert_facts(snapshot, delivered['outcar'], delivered['stdout'])
                    self.assert_facts(snapshot, 4, 3)

    def test_open_native_header_keeps_current_context_separate_from_checked_frontier(self):
        root, context, service = self.make_case(17)
        self.append(root, 'outcar', HEADER + NATIVE_ONE)
        self.append(root, 'stdout', ROW_ONE + ROW_TWO)
        self.assert_facts(self.drain(context, service), 2, 3)
        self.append(root, 'outcar', NATIVE_TWO_HEADER)
        snapshot = self.drain(context, service)
        self.assert_facts(snapshot, 3, 3)
        self.assertEqual(self.metrics(snapshot)['vasp.electronic.dE'].previous.value, 1.)
        self.assertIsNone(self.metrics(snapshot)['vasp.electronic.rms_c'].latest.value)
        self.append(root, 'outcar', NATIVE_TWO_BODY)
        self.assert_facts(self.drain(context, service), 4, 3)

    def test_native_replacement_and_truncation_revoke_cross_generation_match(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                root, context, service = self.make_case(127)
                self.append(root, 'outcar', HEADER + NATIVE_ONE + NATIVE_TWO_HEADER + NATIVE_TWO_BODY)
                self.append(root, 'stdout', ROW_ONE + ROW_TWO)
                original = self.drain(context, service)
                self.assert_facts(original, 4, 3)
                path = root / 'OUTCAR'
                if replacement:
                    changed = root / 'replacement'
                    changed.write_text(HEADER + NATIVE_ONE)
                    changed.replace(path)
                else:
                    path.write_text(HEADER + NATIVE_ONE)
                snapshot = self.drain(context, service)
                old = next(source for source in original.sources if source.role == 'outcar')
                new = next(source for source in snapshot.sources if source.role == 'outcar')
                self.assertEqual(new.generation, old.generation + 1)
                check = next(item for item in snapshot.verification if item.domain == 'electronic-row')
                self.assertEqual(check.status, 'UNRESOLVED')
                self.assertIsNone(check.checked_frontier)
                self.assertEqual((check.observed_frontier, check.native_frontier), (2, 1))
                for series in snapshot.metrics:
                    self.assertEqual(series.latest.qualification, 'source-local')
                    self.assertIsNone(series.latest.criterion)
                self.assertIn('OUTCAR SCF 1/8', self.assert_frame(snapshot))

    def test_electronic_reset_to_empty_preserves_the_scientific_frame(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                root, context, service = self.make_case(127)
                self.append(root, 'outcar', HEADER + NATIVE_ONE)
                self.append(root, 'stdout', ROW_ONE)
                original = self.drain(context, service)
                self.assert_facts(original, 2, 2)
                path = root / 'stdout.log'
                if replacement:
                    changed = root / 'empty-replacement'
                    changed.touch()
                    changed.replace(path)
                else:
                    path.write_text('')
                snapshot = self.drain(context, service)
                self.assertEqual(snapshot.context.binding_status, 'BOUND')
                self.assertEqual(snapshot.status, 'SOURCE_UNRESOLVED')
                self.assertFalse(snapshot.metrics)
                self.assertEqual(next(source.generation for source in snapshot.sources
                                      if source.role == 'stdout'), 2)
                text = self.assert_frame(snapshot)
                self.assertIn('Target N/A · static', text)
                self.assertIn('EDIFF 1E-6 eV', text)

    def test_new_native_segment_cannot_retain_old_context_or_current_values(self):
        root, context, service = self.make_case(127)
        self.append(root, 'outcar', HEADER + NATIVE_ONE + NATIVE_TWO_HEADER + NATIVE_TWO_BODY)
        self.append(root, 'stdout', ROW_ONE + ROW_TWO)
        self.assert_facts(self.drain(context, service), 4, 3)
        self.append(root, 'outcar', ' vasp.6.6.1 new invented segment\n')
        snapshot = self.drain(context, service)
        self.assertEqual(snapshot.segment, 2)
        self.assertFalse(snapshot.iteration)
        self.assertFalse(snapshot.metrics)
        self.assertFalse(snapshot.settings)
        text = self.assert_frame(snapshot)
        self.assertIn('OUTCAR SCF unavailable', text)
        self.assertNotIn('OUTCAR SCF 2/8', text)
        self.assertNotIn('EDIFF 1E-6 eV', text)
        self.assertNotIn('Target N/A · static', text)

    def test_hand_computable_force_norms_wait_for_closing_separator(self):
        root, context, service = self.make_case(17, constraints=True)
        self.append(root, 'outcar', HEADER + NATIVE_ONE + FORCE_OPEN)
        self.append(root, 'stdout', ROW_ONE)
        pending = self.drain(context, service)
        self.assertFalse(any(key.startswith('vasp.force.') for key in self.metrics(pending)))
        self.append(root, 'outcar', FORCE_CLOSE)
        snapshot = self.drain(context, service)
        metrics = self.metrics(snapshot)
        # sqrt(.3**2 + .4**2) = .5 for movable H; fixed He has norm 2.
        for scope, value in (('all', 2.), ('free', .5), ('fixed', 2.)):
            metric = metrics['vasp.force.maximum_atomic_norm.' + scope].latest
            self.assertEqual(metric.value, value)
            self.assertEqual(metric.unit, 'eV/Angstrom')
            self.assertEqual(metric.context[0].index, 1)
            self.assertIsNone(metric.criterion)  # Static has no ionic force target.
        self.assertIn('Target N/A · static', self.assert_frame(snapshot))


if __name__ == '__main__':
    unittest.main()
