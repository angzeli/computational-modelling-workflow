"""Receipt qualifications survive display-cell truncation before optional titles."""
import unittest

from cmw.progress.formatting import cell_width
from cmw.progress.receipt import ReceiptCaption


class ReceiptCaptionTests(unittest.TestCase):
    def test_truncation_priority_in_terminal_cells(self):
        caption = ReceiptCaption('研究-node', '23:59:58 UTC', '12s ago')
        full = '研究-node: Last reported · received 23:59:58 UTC · 12s ago'
        self.assertEqual(caption.format('SCF progress', cell_width(full) + 7), full + ' · SCF…')
        self.assertEqual(caption.format('SCF', cell_width(full)), full)
        self.assertEqual(caption.format('SCF', cell_width(full) - 1), '研究-node: Last reported · 12s ago')
        self.assertEqual(caption.format('SCF', cell_width('研究-node: Last reported')), '研究-node: Last reported')
        self.assertEqual(caption.format('SCF', len('Last reported')), 'Last reported')
        for width in range(len('Last reported'), 121):
            with self.subTest(width=width):
                text = caption.format('長' * 150, width)
                self.assertLessEqual(cell_width(text), width)
                self.assertIn('Last reported', text)

    def test_missing_receipt_has_no_fabricated_time_or_age(self):
        caption = ReceiptCaption('非' * 100, None, None)
        for width in (65, 77, 80, 120):
            text = caption.format('Scientific progress', width)
            self.assertIn('Last reported · receipt unknown', text)
            self.assertLessEqual(cell_width(text), width)
            self.assertNotIn(' ago', text)
        self.assertEqual(caption.format('SCF', 13), 'Last reported')
        self.assertEqual(caption.format('SCF', 12), '~')
