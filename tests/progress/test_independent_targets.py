"""Targets remain source-backed observations before measurements exist."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from cmw.progress.card import card_text
from cmw.progress.formatting import progress_text
from cmw.progress.models import ProgressSnapshot, RunContext, SettingObservation, SourceDescriptor
from cmw.progress.service import ObservationService
from cmw.progress.vasp_card import vasp_card_groups
from tests.periodic.vasp.result_fixtures import native_texts


def card(*settings, calculation='static', status='OBSERVED'):
    snapshot = ProgressSnapshot(RunContext('independent-targets', 'VASP'), 'vasp-live-6.6.1',
        status=status, calculation=calculation, settings=settings)
    snapshot = replace(snapshot, card_groups=vasp_card_groups(snapshot))
    return snapshot, progress_text(snapshot, width=77, now=100)


class IndependentTargetTests(unittest.TestCase):
    def test_effective_ediff_exists_without_any_measurement(self):
        snapshot, text = card(SettingObservation('EDIFF', 1e-6, 'OUTCAR current segment'))
        self.assertIn('|dE| —', text)
        self.assertIn('|d eps| —', text)
        self.assertIn('rms —', text)
        self.assertIn('EDIFF (both) <1e-06 eV', text)
        self.assertFalse(snapshot.metrics)
        self.assertNotIn('BELOW', text)
        self.assertNotIn('reached', text)
        self.assertEqual(snapshot.scientific_validation, 'not-assessed')

    def test_effective_setting_precedes_provisional_in_either_order(self):
        effective = SettingObservation('EDIFF', 1e-6, 'OUTCAR current segment')
        provisional = SettingObservation('EDIFF', 1e-4, 'Declared INCAR', 'provisional')
        for settings in ((effective, provisional), (provisional, effective)):
            with self.subTest(settings=settings):
                _, text = card(*settings)
                self.assertIn('EDIFF (both) <1e-06 eV', text)
                self.assertNotIn('<0.0001', text)
                self.assertNotIn('<1e-06 eV*', text)

    def test_provisional_target_and_effective_input_discrepancy_remain_visible(self):
        _, provisional = card(SettingObservation('EDIFF', 1e-6, 'Declared INCAR', 'provisional'))
        self.assertIn('EDIFF (both) <1e-06 eV*', provisional)
        self.assertIn('* provisional settings', provisional)
        snapshot, mismatch = card(SettingObservation('EDIFF', 1e-6, 'OUTCAR current segment',
            compared_value=1e-4, discrepancy=True))
        self.assertIn('EDIFF (both) <1e-06 eV', mismatch)
        self.assertIn('settings differ: EDIFF', mismatch)
        details = progress_text(snapshot, detailed=True, now=100)
        self.assertIn('compared input: 0.0001; discrepancy: True', details)
        self.assertIn('OUTCAR current segment', details)

    def test_zero_ediff_is_fixed_iterations_without_metrics(self):
        _, text = card(SettingObservation('EDIFF', 0, 'OUTCAR current segment'))
        self.assertIn('EDIFF=0 · fixed iterations', text)
        self.assertNotIn('<0', text)

    def test_invalid_or_unqualified_ediff_is_not_a_threshold(self):
        for value in (-1e-6, None, '1e-6', True, float('nan'), float('inf')):
            with self.subTest(value=value):
                _, text = card(SettingObservation('EDIFF', value, 'OUTCAR current segment'))
                self.assertIn('EDIFF unavailable', text)
                self.assertNotIn('EDIFF (both) <', text)
        for qualification in ('other-segment', 'conflict', 'unsupported', 'unavailable'):
            with self.subTest(qualification=qualification):
                _, text = card(SettingObservation('EDIFF', 1e-6, 'OUTCAR', qualification))
                self.assertIn('EDIFF unavailable', text)
        _, text = card(SettingObservation('EDIFF', 1e-6, 'OUTCAR'), status='UNSUPPORTED')
        self.assertIn('EDIFF unavailable', text)

    def test_absent_settings_never_invent_targets(self):
        _, text = card(calculation='fixed-cell-relaxation')
        self.assertIn('EDIFF unavailable', text)
        self.assertIn('Target unavailable', text)
        self.assertNotIn('provisional', text)

    def test_force_target_exists_conditionally_before_force_measurement(self):
        for qualification in ('effective', 'provisional'):
            with self.subTest(qualification=qualification):
                snapshot, text = card(SettingObservation('EDIFFG', -.02, 'Declared target', qualification),
                    calculation='fixed-cell-relaxation')
                expected = 'Conditional <0.02 eV/Å' + ('*' if qualification == 'provisional' else '')
                self.assertIn(expected, text)
                self.assertIn('— (not observed)', text)
                self.assertFalse(snapshot.metrics)
                self.assertNotIn('BELOW', text)

    def test_static_positive_zero_and_invalid_force_target_semantics(self):
        _, static = card(SettingObservation('EDIFFG', -.02, 'OUTCAR current segment'))
        self.assertIn('Target N/A · static', static)
        self.assertNotIn('<0.02', static)
        for value, expected in ((.02, 'Target N/A · ionic energy'), (0, 'Target N/A · EDIFFG=0')):
            with self.subTest(value=value):
                _, text = card(SettingObservation('EDIFFG', value, 'OUTCAR current segment'),
                    calculation='fixed-cell-relaxation')
                self.assertIn(expected, text)
                self.assertNotIn('Conditional <', text)
        for value in (None, '-0.02', True, float('nan'), float('inf')):
            with self.subTest(value=value):
                _, text = card(SettingObservation('EDIFFG', value, 'OUTCAR current segment'),
                    calculation='fixed-cell-relaxation')
                self.assertIn('Target unavailable', text)
        for calculation, status in ((None, 'OBSERVED'), ('fixed-cell-relaxation', 'UNSUPPORTED')):
            with self.subTest(calculation=calculation, status=status):
                _, text = card(SettingObservation('EDIFFG', -.02, 'OUTCAR current segment'),
                    calculation=calculation, status=status)
                self.assertIn('Target unavailable', text)

    def test_conflicting_native_settings_cannot_restore_a_target(self):
        native = native_texts(ediff='1e-6')
        prefix = native['OUTCAR'].split('--------------------------------------- Iteration', 1)[0]
        for outcar, oszicar in ((prefix + ' EDIFF = 1e-4\n', ''),
                               (native['OUTCAR'] + ' EDIFF = 1e-4\n', native['OSZICAR'])):
            with self.subTest(metrics_present=bool(oszicar)), tempfile.TemporaryDirectory(prefix='cmw-target-conflict-') as temporary:
                root = Path(temporary).resolve()
                (root / 'OUTCAR').write_text(outcar)
                (root / 'OSZICAR').write_text(oszicar)
                context = RunContext('target-conflict', 'VASP', tuple(
                    SourceDescriptor(role.lower(), str(root / role)) for role in ('OUTCAR', 'OSZICAR')))
                service = ObservationService()
                try:
                    snapshot = service.poll(context)
                    setting = next(setting for setting in snapshot.settings if setting.name == 'EDIFF')
                    self.assertEqual(setting.qualification, 'conflict')
                    self.assertTrue(all(series.latest.criterion is None for series in snapshot.metrics))
                    text = progress_text(snapshot, width=77)
                    self.assertIn('EDIFF unavailable', card_text(snapshot, width=77, now=100))
                    self.assertNotIn('EDIFF (both) <', text)
                    self.assertIn('conflict', progress_text(snapshot, detailed=True))
                finally:
                    service.close()

    def test_conflicting_declared_incar_does_not_supply_a_provisional_target(self):
        with tempfile.TemporaryDirectory(prefix='cmw-incar-conflict-') as temporary:
            root = Path(temporary).resolve()
            (root / 'OUTCAR').write_text('')
            (root / 'OSZICAR').write_text('')
            (root / 'INCAR').write_text('EDIFF = 1e-6\nEDIFF = 1e-4\n')
            context = RunContext('incar-conflict', 'VASP', tuple(
                SourceDescriptor(role.lower(), str(root / role), optional=role == 'INCAR')
                for role in ('OUTCAR', 'OSZICAR', 'INCAR')))
            service = ObservationService()
            try:
                snapshot = service.poll(context)
                setting = next(setting for setting in snapshot.settings if setting.name == 'EDIFF')
                self.assertEqual(setting.qualification, 'conflict')
                self.assertFalse(snapshot.metrics)
                self.assertIn('EDIFF unavailable', progress_text(snapshot, width=77))
                self.assertNotIn('EDIFF (both) <', progress_text(snapshot, width=77))
            finally:
                service.close()

    def test_real_adapter_settings_reach_card_before_native_metrics(self):
        with tempfile.TemporaryDirectory(prefix='cmw-targets-') as temporary:
            root = Path(temporary).resolve()
            native = native_texts(relaxation=True, ediff='1e-6')
            prefix = native['OUTCAR'].split('--------------------------------------- Iteration', 1)[0]
            (root / 'OUTCAR').write_text(prefix)
            (root / 'OSZICAR').write_text('')
            context = RunContext('target-prefix', 'VASP', tuple(
                SourceDescriptor(role.lower(), str(root / role)) for role in ('OUTCAR', 'OSZICAR')))
            service = ObservationService()
            try:
                snapshot = service.poll(context)
                self.assertFalse(snapshot.metrics)
                self.assertEqual(snapshot.calculation, 'fixed-cell-relaxation')
                ediff = next(setting for setting in snapshot.settings if setting.name == 'EDIFF')
                self.assertEqual(ediff.qualification, 'effective')
                self.assertEqual(ediff.value, 1e-6)
                text = progress_text(snapshot, width=77)
                self.assertIn('EDIFF (both) <1e-06 eV', text)
                self.assertIn('Conditional <0.02 eV/Å', text)
                self.assertIn('|dE| —', text)
                self.assertIn('|d eps| —', text)
                self.assertIn('rms —', text)
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
