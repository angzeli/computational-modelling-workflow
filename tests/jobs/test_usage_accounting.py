"""Resource labels from injected bytes only; no live process inspection."""
import unittest
from cmw.jobs import cli
from tests.jobs.test_usage_ui import usage, machine


class UsageAccountingTests(unittest.TestCase):
    def test_compact_line_has_sum_coverage_age_without_extra_rows(self):
        value = usage(rss_bytes=8367669248, total_members=11, memory_members=11,
                      observed_members=11, expected_vasp_ranks=8, observed_vasp_ranks=8,
                      coverage_quality='complete', scope='verified payload session', age_seconds=2)
        text = cli.usage_summary(value, width=72)
        self.assertIn('RSSΣ', text)
        self.assertIn('11p', text)
        self.assertIn('8/8', text)
        self.assertIn('2s', text)
        self.assertNotIn('\n', text)
        self.assertLessEqual(len(text), 72)
        self.assertLessEqual(len(cli.usage_summary(value, width=56)), 56)

    def test_partial_and_unavailable_rank_counts_are_not_zero(self):
        value = usage(expected_vasp_ranks=8, observed_vasp_ranks=None,
                      coverage_quality='partial', scope='verified payload session')
        text = cli.usage_summary(value, width=72)
        self.assertIn('—/8', text)
        self.assertIn('partial', text)
        self.assertNotIn('0/8', text)
        self.assertNotIn('0s', cli.usage_summary(usage(age_seconds=None)))

    def test_detail_separates_rss_footprint_scope_and_timestamp(self):
        value = usage(rss_bytes=8367669248, footprint_bytes=20779474272,
                      footprint_quality='fresh', footprint_members=11,
                      footprint_source='proc_pid_rusage.ri_phys_footprint',
                      memory_source='psutil.Process.memory_info().rss',
                      total_members=11, memory_members=11, observed_members=11,
                      scope='verified payload session', coverage_quality='complete',
                      expected_vasp_ranks=8, observed_vasp_ranks=8)
        text = cli.usage_detail(value)
        self.assertIn('7.8 GiB RSSΣ', text)
        self.assertIn('19.4 GiB', text)
        self.assertIn('footprint', text)
        self.assertIn('verified payload session', text)
        self.assertIn('8/8', text)
        self.assertIn('Sample time:', text)
        self.assertIn('shared pages', text)
        self.assertIn('not unique physical RAM', text)
        self.assertIn('psutil.Process.memory_info().rss', text)
        self.assertNotIn('0.0 GiB', cli.usage_detail(usage(footprint_bytes=None)))

    def test_machine_formula_label_preserves_numbers_and_width(self):
        text = cli.machine_text(machine())
        self.assertIn('23.8 / 36.0 GiB', text)
        self.assertIn('psutil', text)
        self.assertIn('T−avail', text)
        self.assertLessEqual(len(text), 72)
        value = machine()
        value.update(ram_used_bytes=10**9, ram_total_bytes=2**30)
        self.assertIn('0.9 / 1.0 GiB', cli.machine_text(value))

    def test_stale_values_are_never_described_as_fresh(self):
        text = cli.usage_summary(usage(stale=True, age_seconds=9), width=72)
        self.assertIn('stale', text.lower())
        self.assertIn('9s', text)
        self.assertEqual(cli.usage_cells(None), ('—', '—'))
