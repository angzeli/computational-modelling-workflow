"""Actual Textual layouts using synthetic captured evidence and isolated state."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import asyncio
import tempfile
import unittest
from unittest.mock import Mock, patch

from cmw.jobs.clipboard import CopyResult
from cmw.jobs.snapshot import render_snapshot
from cmw.jobs.store import Store
from cmw.progress.models import ProgressEvent, SettingObservation
from tests.jobs.ui_polish_acceptance import NOW, SIZES, scenario_projection, screenshot_text
from tests.progress.test_scientific_card import example

try:
    from cmw.jobs.copy_ui import CopyPreview
    from cmw.jobs.tui import JobsApp
    from textual.widgets import DataTable, Select, Static, TextArea
except ImportError:
    JobsApp = None


async def scientific_card_views(root, renders=None):
    store = Store(root / 'state')
    jobs = [store.add(argv=['/unexecuted/synthetic'], cwd=root, engine='VASP', hold=True,
                     name=f'Synthetic electronic observation {index + 1}') for index in range(8)]
    baseline = store.snapshot(now=NOW)
    state = deepcopy(baseline)
    state['controller'].update(online=True, stale=False, dispatch=True, heartbeat=NOW,
                               age_seconds=0, reason='Synthetic controller')
    for job in state['jobs']:
        job.update(status='Done', order=None, elapsed=45.0)
    selected = state['jobs'][-1]
    selected.update(status='Run', elapsed=150.0)
    projection = scenario_projection(state, 'no_external_complete')
    projection['job_usage'][selected['attempt_id']]['cpu_cores'] = 7.91
    captured = example()
    context = replace(captured.context, binding_id='synthetic-ui-card', attempt_id=selected['attempt_id'],
                      job_id=selected['id'], jobs_state=str(store.root))
    captured = replace(captured, context=context, last_new_evidence_at=NOW - 12, last_success_at=NOW,
        metrics=tuple(replace(item, latest=replace(item.latest, observed_at=NOW - 12))
                      for item in captured.metrics))
    variants = {
        'source-local': replace(captured, verification=(replace(captured.verification[0], status='PENDING', checked_frontier=None),)),
        'partial': replace(captured, verification=(replace(captured.verification[0], status='PARTIAL', checked_frontier=6),)),
        'matched': captured,
        'conflict': replace(captured, verification=(replace(captured.verification[0], status='CONFLICT',
                                      reason='Synthetic complete-row disagreement'),), read_health='ERROR'),
    }
    variants['cached-error'] = replace(variants['partial'],
        events=(ProgressEvent('native-warning', 'native-fatal', None, 1, message='Synthetic native fatal text'),),
        settings=(SettingObservation('EDIFF', 1e-5, 'OUTCAR effective', compared_value=1e-6, discrepancy=True),))
    operational = dict(observed_execution='VERIFIED_WORKER', age_seconds=0, stale=False,
                       reconciliation_required=False, evidence={})
    views = []
    if renders:
        renders.mkdir(parents=True, exist_ok=True)
    backend = Mock(return_value=CopyResult('LOCAL_ACCEPTED', 'Synthetic clipboard'))
    app = JobsApp(store, clock=lambda: NOW, clipboard_backend=backend)
    app.selected_id = selected['display_id']
    app.activity_projection = deepcopy(projection)
    app.job_usage = deepcopy(projection['job_usage'])
    with patch.object(store, 'snapshot', side_effect=lambda **_: deepcopy(state)), \
         patch.object(store, 'transaction', side_effect=AssertionError('Observation must not mutate Jobs state')), \
         patch.object(JobsApp, 'collect_activity'), patch.object(JobsApp, 'collect_progress'), \
         patch.object(JobsApp, 'collect_execution'), patch.object(JobsApp, 'selected_execution', return_value=operational), \
         patch('subprocess.Popen', side_effect=AssertionError('No process launch in synthetic portal acceptance')), \
         patch('time.time', return_value=NOW):
        async with app.run_test(size=SIZES[0]) as pilot:
            table = app.query_one('#table', DataTable)
            table.move_cursor(row=app.row_ids.index(selected['display_id']), animate=False)
            for scenario, snapshot in variants.items():
                app.progress_snapshot = snapshot
                app.progress_error = 'Synthetic disconnected source; cached observations' if scenario == 'cached-error' else None
                evidence_before = snapshot.to_dict()
                for size in (*SIZES, (50, 30)):
                    await pilot.resize_terminal(*size)
                    app.refresh_state()
                    app.render_progress()
                    await pilot.pause()
                    svg = app.export_screenshot()
                    text = screenshot_text(svg)
                    if renders:
                        (renders / f'scientific-{scenario}-{size[0]}x{size[1]}.svg').write_text(svg)
                    card = str(app.query_one('#progress', Static).render())
                    assert table.size.height - table.header_height >= (2 if scenario == 'cached-error' or size[0] < 60 else 3), (scenario, size, 'fewer than three useful job rows')
                    assert app.query_one('#shortcuts').region.bottom <= size[1], (scenario, size, 'footer clipped')
                    assert app.query_one('#progress').region.bottom <= size[1], (scenario, size, 'scientific card clipped')
                    assert app.query_one('#progress').size.height >= (5 if size[0] < 60 else 6), (scenario, size, 'scientific rows clipped')
                    assert all(word in text for word in ('SCF', 'FORCE', 'ELECTRONIC', 'data', '791%', 'RSS')), (scenario, size)
                    if size[0] >= 60:
                        assert all(word in card for word in ('@SCF8', 'OUTCAR SCF 9/300', 'EDIFF (both)', 'static'))
                    framed = size == (140, 40)
                    assert ('┌─' in card) == framed
                    assert app.query_one('#progress').has_class('framed') == framed
                    if framed:
                        assert app.query_one('#progress').styles.padding.left == 0
                        assert app.query_one('#progress').styles.border_left[0] == ''
                        assert app.query_one('#selected').styles.border_left[0] == 'solid'
                    region = app.query_one('#progress').region
                    app.render_progress()
                    await pilot.pause()
                    assert str(app.query_one('#progress').render()) == card
                    assert app.query_one('#progress').region == region
                    assert '(native' not in card and 'allowed' not in card
                    if scenario == 'conflict':
                        assert 'CONFLICT' in text and 'ERROR' in text
                    elif scenario in ('partial', 'cached-error') and size[0] >= 60:
                        assert 'outcar checked 6' in text and '8 source-local' in text
                    if scenario == 'cached-error' and size[0] >= 60:
                        assert all(word in text for word in ('cached', 'native warnings', 'settings differ', '8 source-local'))
                    if scenario == 'matched' and framed:
                        capture = app.capture_copy_snapshot()
                        await pilot.press('y')
                        assert isinstance(app.screen, CopyPreview)
                        for mode in ('summary', 'diagnostics', 'json'):
                            app.screen.query_one(Select).value = mode
                            await pilot.pause()
                            copied = app.screen.query_one(TextArea).text
                            assert copied == render_snapshot(capture, mode=mode)
                            assert not any(char in copied for char in '┌┐└┘├┤┴')
                        await pilot.press('escape')
                        widget = app.query_one('#progress', Static)
                        await pilot.mouse_down('#progress', offset=(2, 1))
                        await pilot.hover('#progress', offset=(5, 1))
                        await pilot.mouse_up('#progress', offset=(5, 1))
                        selection = app.screen.get_selected_text()
                        assert selection
                        app.progress_snapshot = replace(snapshot, settings=(SettingObservation(
                            'synthetic warning ' * 12, 1, 'fixture', discrepancy=True),))
                        app.render_progress()
                        await pilot.pause()
                        assert str(widget.render()) == card
                        assert widget.has_class('framed')
                        assert widget.region == region
                        await pilot.press('ctrl+c')
                        await app.workers.wait_for_complete()
                        await pilot.pause()
                        backend.assert_called_once_with(selection)
                        assert app.screen is app.screen_stack[0]
                        app.screen.clear_selection()
                        await pilot.pause()
                        assert not widget.has_class('framed')
                        assert '┌─' not in str(widget.render())
                        app.progress_snapshot = snapshot
                        app.render_progress()
                        await pilot.pause()
                    assert snapshot.to_dict() == evidence_before
                    assert app.selected_id == selected['display_id']
                    views.append(dict(scenario=scenario, size=size, useful_rows=table.size.height-table.header_height,
                                      card=card, footer_bottom=app.query_one('#shortcuts').region.bottom))
            await pilot.press('q')
    assert store.snapshot(now=NOW) == baseline, 'Synthetic state changed during observation'
    return views


@unittest.skipIf(JobsApp is None, 'Optional Jobs console not installed')
class ScientificCardUiTests(unittest.TestCase):
    def test_synthetic_source_verification_cards_at_required_and_narrow_sizes(self):
        with tempfile.TemporaryDirectory(prefix='cmw-scientific-card-ui-') as directory:
            views = asyncio.run(scientific_card_views(Path(directory)))
        self.assertEqual(len(views), 25)
        self.assertTrue(all(view['useful_rows'] >= 3 for view in views if view['scenario'] != 'cached-error' and view['size'][0] >= 60))


if __name__ == '__main__':
    unittest.main()
