"""Synthetic shared scanners; no live buffers or scientific process required."""
from __future__ import annotations

from copy import deepcopy
import unittest

from cmw.periodic.vasp.analysis_evidence import (
    correspondence_scan, duplicate_scan, parse_loop_timing,
)
from cmw.periodic.vasp.results import number


def rows(count):
    return [{'label': i + 1, 'algorithm': 'DAV', 'energy': number('-1.00000000'),
             'dE': number('-.100000E-8'), 'd_eps': number('-.200000E-8'),
             'rms': number('.100000E-3')} for i in range(count)]


class SharedEvidenceTests(unittest.TestCase):
    def test_native_timer_units_sign_and_precision(self):
        result = parse_loop_timing(' LOOP+: cpu time 1.2500D+02: real time 130.0000\n')
        self.assertEqual(result['kind'], 'LOOP+')
        self.assertEqual(result['cpu'], number('1.2500D+02'))
        self.assertEqual(result['wall']['value'], 130)
        for text in ('LOOP: cpu time -1: real time 2',
                     'LOOP: cpu time NaN: real time 2',
                     'LOOP: cpu time 1: real time Inf',
                     'LOOP: 1 2'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_loop_timing(text)

    def test_complete_history_is_not_limited_to_live_buffers(self):
        native = rows(1200)
        energies = {'free_energy': number('-1.00000000'), 'sigma_to_zero': number('-1.00000000')}
        evaluation = {'rows': native, 'native': 1, 'complete': True, 'energies': energies}
        item = {'rows': deepcopy(native), 'summary': {'native_ionic_label': 1, **energies}}
        status, matched, reason = correspondence_scan(evaluation, item)
        self.assertEqual(status, 'matched', reason)
        self.assertEqual(len(matched), 1200)

    def test_buffered_prefix_pending_but_contradiction_conflicts(self):
        native = rows(3)
        evaluation = {'rows': native, 'native': 1, 'complete': False, 'energies': {}}
        item = {'rows': deepcopy(native[:2]), 'summary': None}
        status, matched, _ = correspondence_scan(evaluation, item)
        self.assertEqual((status, len(matched)), ('pending', 2))
        item['rows'][1]['energy'] = number('-2.00000000')
        self.assertEqual(correspondence_scan(evaluation, item)[0], 'conflict')

    def test_duplicate_partial_fields_and_complete_conflict(self):
        first = {'rows': rows(3), 'summary': None}
        second = {'rows': rows(2), 'summary': None}
        second['rows'][0]['rms_c'] = number('.100000E-3')
        state, checked, fields, reason = duplicate_scan(first, second)
        self.assertEqual((state, len(checked)), ('PARTIAL', 2))
        self.assertNotIn('rms_c', fields)
        self.assertIn('absent', reason)
        second['rows'][1]['dE'] = number('-.200000E-8')
        self.assertEqual(duplicate_scan(first, second)[0], 'CONFLICT')


if __name__ == '__main__':
    unittest.main()
