"""Independent timing semantics and arbitrary history context contracts."""
from dataclasses import replace
import unittest
from cmw.progress.models import HistoryEntry, HistoryPage, IterationLevel, RunContext, Timing


class HistoryContractTests(unittest.TestCase):
    def test_cpu_wall_observer_and_missing_are_distinct(self):
        wall = Timing('wall', 'Wall', 7.5, source='native', completeness='COMPLETE')
        cpu = replace(wall, timing_id='cpu', value=22, clock='cpu')
        seen = Timing('seen', 'Observed', source='observer', lower_bound=1.25,
                      quantity='observation-interval', clock_id='session-one')
        missing = Timing('missing', 'Unavailable')
        entry = HistoryEntry('evaluation', 1, timings=(wall, cpu, seen, missing))
        data = HistoryPage(RunContext('binding', 'FICTIONAL'), entries=(entry,)).to_dict()
        times = data['entries'][0]['timings']
        self.assertEqual([t['value'] for t in times], [7.5, 22, None, None])
        self.assertEqual([t['clock'] for t in times[:2]], ['wall', 'cpu'])
        self.assertEqual(times[2]['lower_bound'], 1.25)
        self.assertEqual(times[3]['source'], 'unavailable')

    def test_invalid_intervals_are_not_clamped_to_zero(self):
        for value in (-1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                Timing('bad', 'Bad', value, source='native')
        with self.assertRaises(ValueError):
            Timing('reverse', 'Reversed', source='observer', lower_bound=3, upper_bound=2)
        with self.assertRaises(ValueError):
            Timing('invented', 'Unavailable', 0.0)
        self.assertEqual(Timing('zero', 'Printed zero', 0.0, source='native').value, 0.0)

    def test_nested_context_and_repeated_labels_keep_distinct_identity(self):
        path = tuple(IterationLevel(kind, n, str(n)) for kind, n in
                     [('geometry', 7), ('outer', 3), ('inner', 12)])
        first = HistoryEntry('first', 1, path[:1], native_label='2', segment=1)
        second = HistoryEntry('second', 5, path[:1], native_label='2', segment=2)
        child = HistoryEntry('nested', 3, path, parent_id='outer', kind='inner')
        data = HistoryPage(RunContext('binding', 'FICTIONAL'),
                           entries=(first, second, child)).to_dict()
        self.assertEqual(len(data['entries'][2]['context']), 3)
        self.assertEqual(data['entries'][2]['parent_id'], 'outer')
        self.assertNotEqual(data['entries'][0]['entry_id'], data['entries'][1]['entry_id'])

    def test_loading_is_not_complete_history(self):
        data = HistoryPage(RunContext('binding', 'FICTIONAL'), status='LOADING',
                           has_more=True, retained=3).to_dict()
        self.assertEqual(data['coverage'], 'PARTIAL')
        self.assertIsNone(data['total'])
        self.assertTrue(data['has_more'])
