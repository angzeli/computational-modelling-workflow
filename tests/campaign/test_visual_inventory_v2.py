"""Inventory binding cannot turn missing/duplicate exports into visual acceptance."""
import json
from pathlib import Path
import tempfile
import unittest

from tests.campaign.visual_inventory_v2 import inventory, historical_mapping


class VisualInventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-visual-inventory-')
        self.addCleanup(temporary.cleanup)
        self.roots = {name: Path(temporary.name)/name for name in ('history','views','active_writer')}
        for root in self.roots.values():
            root.mkdir()

    def test_missing_required_ids_remain_explicit_and_extra_failure_is_retained(self):
        (self.roots['views']/'input-failure.svg').write_text('<svg/>')
        rows = inventory(self.roots)
        self.assertEqual(sum(row['phase']=='settled' and row['source'] is None for row in rows),32)
        self.assertEqual(rows[-1]['phase'],'failure')
        self.assertTrue(rows[-1]['required'])

    def test_same_export_in_nested_and_child_reports_deduplicates(self):
        root = self.roots['history']
        child = root/'child'; child.mkdir()
        (child/'frame.svg').write_text('<svg/>')
        frame = {'required_id':'local-static-80x24','size':[80,24],'image':'frame.svg'}
        (child/'result.json').write_text(json.dumps({'frames':[frame]}))
        (root/'result.json').write_text(json.dumps({'compact_core':[{'directory':str(child),'frames':[frame]}]}))
        rows = inventory(self.roots)
        found = next(row for row in rows if row['id']==frame['required_id'])
        self.assertEqual(found['source'],str((child/'frame.svg').resolve()))
        self.assertEqual(len(rows),32)

    def test_distinct_exports_with_same_required_id_are_refused(self):
        root = self.roots['history']
        for name in ('a','b'):
            child=root/name; child.mkdir()
            (child/'frame.svg').write_text('<svg/>')
            (child/'result.json').write_text(json.dumps({'required_id':'local-static-80x24',
                'size':[80,24],'image':'frame.svg'}))
        with self.assertRaisesRegex(AssertionError,'Duplicate distinct'):
            inventory(self.roots)

    def test_unknown_historical_scenario_is_refused(self):
        with self.assertRaisesRegex(AssertionError,'Unmapped historical'):
            historical_mapping({'total':1,'frames':[{'id':'unknown','group':'new',
                'view':'new','relative_path':'new.svg','phase':'settled'}]})

    def test_renderer_cli_reports_missing_frames_without_rendering_or_accepting(self):
        from unittest.mock import patch
        from tests.campaign.visual_inventory_v2 import main
        parent = self.roots['history'].parent
        roots = parent/'roots.json'
        roots.write_text(json.dumps({name:str(root) for name,root in self.roots.items()}))
        with patch('sys.argv',['visual-inventory','--roots',str(roots),'--output',str(parent/'render')]):
            self.assertEqual(main(),1)
        result = json.loads((parent/'render'/'manifest.json').read_text())
        self.assertEqual(result['counts']['missing'],32)
        self.assertFalse(result['render_complete'])
        self.assertEqual(result['visual_acceptance'],'PENDING_INDEPENDENT_PER_IMAGE_INSPECTION')
