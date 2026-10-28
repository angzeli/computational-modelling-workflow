"""Synthetic OUTCAR setting grammar; no private output or engine execution."""
from pathlib import Path
import tempfile
import unittest

from cmw.periodic.vasp.results import inspect_result, parse_outcar
from cmw.progress.formatting import progress_text
from cmw.progress.models import RunContext, SourceDescriptor
from cmw.progress.service import ObservationService
from tests.periodic.vasp.result_fixtures import native_texts


ADVICE = """ | For better performance, we recommend the following setting: |
 | NCORE = 2 up to number-of-cores-per-socket                   |
 | NPAR = CPU/NCORE). The default NCORE = 1 can be inefficient. |
 Advice NCORE=8 and NCORE=16 are examples, not input records.
"""
PARAMETERS = " Startparameter for this run:\n"
DISTRIBUTION = " distr: one band on NCORE=1 cores, 8 groups\n"


class VaspSettingContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-setting-context-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.outcar = self.root / 'OUTCAR'
        (self.root / 'OSZICAR').write_text('')
        self.service = ObservationService()
        self.addCleanup(self.service.close)
        self.context = RunContext('synthetic-settings', 'VASP', tuple(
            SourceDescriptor(role.lower(), str(self.root / role))
            for role in ('OUTCAR', 'OSZICAR')), attempt_id='synthetic-attempt')

    def observe(self, text):
        self.service.close()
        self.service = ObservationService()
        self.addCleanup(self.service.close)
        self.outcar.write_text(text)
        return self.service.poll(self.context)

    @staticmethod
    def settings(snapshot):
        return {item.name: item for item in snapshot.settings}

    def batch(self):
        return parse_outcar(self.outcar)[0]

    def append(self, text):
        with self.outcar.open('a') as stream:
            stream.write(text)
        return self.service.poll(self.context)

    def test_advice_before_and_after_distribution_never_becomes_effective(self):
        for text in (ADVICE + DISTRIBUTION, DISTRIBUTION + ADVICE,
                     ADVICE + DISTRIBUTION + ADVICE):
            with self.subTest(text=text):
                snapshot = self.observe(' vasp.6.6.1 synthetic\n' + text)
                settings = self.settings(snapshot)
                self.assertEqual(set(settings), {'NCORE'})
                self.assertEqual(settings['NCORE'].value, 1)
                self.assertEqual(settings['NCORE'].qualification, 'effective')
                self.assertNotEqual(snapshot.status, 'SOURCE_CONFLICT')
                ref = settings['NCORE'].evidence[0]
                self.assertIn('distr:', text.splitlines()[ref.line_start - 2])
                segment = self.batch()[0]
                self.assertEqual(set(segment['settings']), {'NCORE'})
                self.assertEqual(segment['settings']['NCORE']['value'], 1)
                self.assertFalse(segment['conflicts'])

    def test_line_leading_advice_is_not_a_native_parameter_section(self):
        for prefix in ('', PARAMETERS):
            with self.subTest(prefix=prefix):
                snapshot = self.observe(' vasp.6.6.1 synthetic\n' + prefix +
                    ' NCORE = 2 up to number-of-cores-per-socket\n' +
                    ' NPAR = CPU/NCORE).\n' + DISTRIBUTION)
                # Within the actual parameter section, a malformed NPAR record
                # is a conflict; it never becomes a string-valued setting.
                self.assertEqual(set(self.settings(snapshot)), {'NCORE'})
                self.assertEqual(self.settings(snapshot)['NCORE'].value, 1)
                self.assertNotIn('NPAR', self.batch()[0]['settings'])
        snapshot = self.observe(' vasp.6.6.1 synthetic\n NCORE = 2\n')
        self.assertFalse(snapshot.settings)
        self.assertFalse(self.batch()[0]['settings'])

    def test_echo_and_parameter_fields_do_not_scan_later_prose(self):
        snapshot = self.observe(' vasp.6.6.1 synthetic\n' + DISTRIBUTION +
            ' INCAR:\n NCORE = 1; ALGO = F ! NCORE=4\n POTCAR: synthetic\n' +
            PARAMETERS + ' EDIFF = 1D-6; NELM = 120 description NCORE=8\n' + ADVICE)
        settings = self.settings(snapshot)
        self.assertEqual(set(settings), {'NCORE', 'EDIFF', 'NELM'})
        self.assertEqual(settings['EDIFF'].value, 1e-6)
        segment = self.batch()[0]
        self.assertEqual(segment['echoed_incar']['ALGO']['value'], 'F')
        self.assertFalse(segment['conflicts'])

    def test_malformed_native_values_are_conflicts_not_effective_settings(self):
        for record in ('NCORE = CPU/NCORE).', 'NCORE = 2.5', 'NCORE = T',
                       'NPAR = 2garbage', 'EDIFF = NaN', 'LHFCALC = 0'):
            with self.subTest(record=record):
                snapshot = self.observe(' vasp.6.6.1 synthetic\n' + PARAMETERS + record + '\n')
                self.assertEqual(snapshot.status, 'SOURCE_CONFLICT')
                self.assertFalse(snapshot.settings)
                segment = self.batch()[0]
                self.assertFalse(segment['settings'])
                self.assertEqual(segment['conflicts'][0]['code'], 'malformed_setting')

    def test_real_contradictions_stay_latched_when_later_values_agree(self):
        snapshot = self.observe(' vasp.6.6.1 synthetic\n' + DISTRIBUTION +
            PARAMETERS + ' NCORE = 2\n' + ADVICE + ' NCORE = 1\n')
        self.assertEqual(snapshot.status, 'SOURCE_CONFLICT')
        self.assertEqual(self.settings(snapshot)['NCORE'].qualification, 'conflict')
        conflicts = self.batch()[0]['conflicts']
        self.assertEqual([item['tag'] for item in conflicts], ['NCORE', 'NCORE'])
        self.assertTrue(all(item['reference']['line_start'] for item in conflicts))

    def test_partial_line_is_not_a_setting_until_completed(self):
        snapshot = self.observe(' vasp.6.6.1 synthetic\n' + PARAMETERS + ' NCORE = 1')
        self.assertFalse(snapshot.settings)
        self.assertFalse(self.batch()[0]['settings'])
        snapshot = self.append('6\n' + ADVICE)
        setting = self.settings(snapshot)['NCORE']
        self.assertEqual(setting.value, 16)
        self.assertEqual(setting.evidence[0].line_start, 3)
        self.assertEqual(self.batch()[0]['settings']['NCORE']['value'], 16)

    def test_partial_advice_does_not_conflict_when_completed(self):
        snapshot = self.observe(' vasp.6.6.1 synthetic\n' + DISTRIBUTION + ' | NCORE = 2')
        self.assertEqual(self.settings(snapshot)['NCORE'].value, 1)
        snapshot = self.append(' up to number-of-cores-per-socket |\n')
        self.assertEqual(self.settings(snapshot)['NCORE'].qualification, 'effective')
        self.assertFalse(self.batch()[0]['conflicts'])

    def test_segments_do_not_share_or_resolve_setting_evidence(self):
        self.observe(' vasp.6.6.1 first\n' + DISTRIBUTION + PARAMETERS + ' NCORE = 2\n')
        snapshot = self.append(' vasp.6.6.1 second\n' + ADVICE)
        self.assertEqual(snapshot.segment, 2)
        self.assertFalse(snapshot.settings)
        segments = self.batch()
        self.assertTrue(segments[0]['conflicts'])
        self.assertFalse(segments[1]['settings'])
        snapshot = self.append(DISTRIBUTION)
        setting = self.settings(snapshot)['NCORE']
        self.assertEqual(setting.qualification, 'effective')
        self.assertGreater(setting.evidence[0].line_start, segments[0]['range']['line_end'])
        self.assertTrue(self.batch()[0]['conflicts'])

    def test_replacement_generation_drops_prior_settings_and_conflicts(self):
        before = self.observe(' vasp.6.6.1 first\n' + DISTRIBUTION + PARAMETERS + ' NCORE = 2\n')
        replacement = self.root / 'replacement'
        replacement.write_text(' vasp.6.6.1 replacement\n' + ADVICE)
        replacement.replace(self.outcar)
        after = self.service.poll(self.context)
        self.assertFalse(after.settings)
        self.assertGreater(after.sources[0].generation, before.sources[0].generation)
        self.assertFalse(self.batch()[0]['settings'])
        after = self.append(PARAMETERS + ' NCORE = 4\n')
        self.assertEqual(self.settings(after)['NCORE'].value, 4)
        self.assertEqual(self.settings(after)['NCORE'].qualification, 'effective')

    def test_supported_result_keeps_literal_oracles_with_advice(self):
        native = native_texts()
        self.outcar.write_text(native['OUTCAR'].replace(' INCAR:', ADVICE + DISTRIBUTION + ' INCAR:'))
        (self.root / 'OSZICAR').write_text(native['OSZICAR'])
        result = inspect_result(self.root)
        self.assertTrue(result['mode']['supported'])
        self.assertFalse(result['conflicts'])
        final = result['selected_segment']['evaluations'][0]
        self.assertEqual(final['energies']['free_energy']['value'], -3.0)
        self.assertEqual(final['iterations'][-1]['electronic_free_energy']['value'], -2.10000005)
        self.assertEqual(final['final_electronic_row']['dE']['value'], -5e-8)
        self.assertIsNone(result['policy_assessment'])
        snapshot = self.service.poll(self.context)
        self.assertEqual(snapshot.status, 'OBSERVED')
        self.assertEqual(self.settings(snapshot)['NCORE'].value, 1)
        self.assertEqual(snapshot.scientific_validation, 'not-assessed')

    def test_advice_after_scf_does_not_create_a_change_conflict(self):
        native = native_texts(settings={'NCORE': '1'})
        snapshot = self.observe(native['OUTCAR'] + ADVICE)
        self.assertNotEqual(snapshot.status, 'SOURCE_CONFLICT')
        snapshot = self.append(' NCORE = 2\n')
        self.assertEqual(snapshot.status, 'SOURCE_CONFLICT')
        self.assertEqual(self.settings(snapshot)['NCORE'].qualification, 'conflict')

    def test_header_only_hybrid_remains_explicitly_unsupported_in_bordered_card(self):
        native = native_texts(settings={'LHFCALC': 'T', 'IALGO': '53'})
        prefix = native['OUTCAR'].split('--------------------------------------- Iteration')[0]
        snapshot = self.observe(prefix + ADVICE + DISTRIBUTION)
        self.assertEqual(snapshot.status, 'UNSUPPORTED')
        self.assertIn('unsupported_mode:LHFCALC', snapshot.limitations)
        self.assertIn('unsupported_electronic_mode', snapshot.limitations)
        self.assertFalse(snapshot.metrics)
        self.assertFalse(snapshot.iteration)
        self.assertTrue(all(check.checked_frontier is None for check in snapshot.verification))
        self.assertEqual(self.settings(snapshot)['NCORE'].qualification, 'effective')
        card = progress_text(snapshot, width=100, max_lines=8, framed=True)
        self.assertIn('┌', card)
        self.assertIn('└', card)
        self.assertIn('SCF', card)
        self.assertIn('unsupported', card)
        self.assertNotIn('Configure Sources', card)
        result = inspect_result(self.root)
        self.assertFalse(result['mode']['supported'])
        self.assertIn('unsupported_mode:LHFCALC', result['mode']['reasons'])
        self.assertIsNone(result['policy_assessment'])
