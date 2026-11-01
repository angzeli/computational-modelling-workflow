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
from cmw.progress.formatting import cell_width
from cmw.progress.models import (Criterion, IterationLevel, Metric, MetricSeries,
                                 ProgressEvent, SettingObservation)
from cmw.progress.vasp_card import vasp_card_groups
from tests.jobs.ui_polish_acceptance import NOW, scenario_projection, screenshot_text
from tests.progress.test_scientific_card import example

try:
    from cmw.jobs.copy_ui import CopyPreview
    from cmw.jobs.tui import JobsApp
    from textual.widgets import DataTable, Footer, Select, Static, TextArea
except ImportError:
    JobsApp = None


SIZES = ((77, 24), (80, 24), (85, 26), (90, 25), (95, 27), (100, 30), (140, 40), (80, 24))


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
    electronic_values = {
        'vasp.electronic.dE': (.00015254, '0.00015254', 5e-9),
        'vasp.electronic.d_eps': (.00000069071, '-0.00000069071', 5e-12),
        'vasp.electronic.rms': (.000415, '0.000415', 5e-7),
    }
    captured = replace(captured, metrics=tuple(replace(item, latest=replace(item.latest,
        value=electronic_values[item.latest.semantic_id][0],
        printed=electronic_values[item.latest.semantic_id][1],
        precision=electronic_values[item.latest.semantic_id][2],
        signed_value=float(electronic_values[item.latest.semantic_id][1])))
        if item.latest.semantic_id in electronic_values else item for item in captured.metrics))
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
    variants['cached-error'] = replace(variants['partial'], read_health='ERROR',
        events=(ProgressEvent('native-warning', 'native-fatal', None, 1, message='Synthetic native fatal text'),),
        settings=(SettingObservation('EDIFF', 1e-5, 'OUTCAR effective', compared_value=1e-6, discrepancy=True),))
    provisional = replace(captured, settings=(SettingObservation('EDIFF', 1e-5, 'Declared INCAR', 'provisional'),),
        metrics=tuple(replace(item, latest=replace(item.latest, qualification='provisional')) for item in captured.metrics))
    variants.update({'provisional': replace(provisional, card_groups=vasp_card_groups(provisional)),
                     'long-name': captured, 'missing-metrics': replace(captured, metrics=()),
                     'suspended': captured, 'guard-warning': captured, 'transient': captured, 'status-error': captured,
                     'unverified-execution': captured})
    force = Metric('vasp.force.maximum_atomic_norm.free', 'Fmax free',
        'Synthetic maximum free-atom force norm', 'eV/Angstrom', .014864,
        context=(IterationLevel('evaluation', 26, 'evaluation 26'),), observed_at=NOW - 12,
        criterion=Criterion(.02, 'eV/Angstrom', '<', 'Synthetic effective EDIFFG'))
    force_snapshot = replace(captured, calculation='fixed-cell-relaxation',
                             metrics=(*captured.metrics, MetricSeries(force)))
    variants['force-decimal'] = replace(force_snapshot, card_groups=vasp_card_groups(force_snapshot))
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
                selected['name'] = 'Synthetic long 名称 ΔE ' * 14 if scenario == 'long-name' else 'Synthetic electronic observation 8'
                selected['suspension'] = ({'phase': 'Suspended', 'verification_fresh': True,
                                           'current_suspended_seconds': 12} if scenario == 'suspended' else {})
                app.activity_projection = scenario_projection(state, 'ownership_uncertain' if scenario == 'guard-warning' else 'no_external_complete')
                app.job_usage = deepcopy(projection['job_usage'])
                operational.update(observed_execution='OWNERSHIP_UNVERIFIED' if scenario == 'unverified-execution' else 'VERIFIED_WORKER',
                                   reconciliation_required=scenario == 'unverified-execution')
                app.execution_selection = ('synthetic', selected['id'], selected['attempt_id'])
                app.query_one('#message').update('Checking declared file locations…' if scenario == 'transient' else
                    'Readiness unavailable: synthetic source cannot be read; inspect Details before proceeding.' if scenario == 'status-error' else
                    'Best-effort guard · Q / Ctrl-C detaches; execution state unchanged.')
                app.progress_snapshot = snapshot
                app.progress_error = 'Synthetic disconnected source; cached observations' if scenario == 'cached-error' else None
                evidence_before = snapshot.to_dict()
                for size in SIZES:
                    await pilot.resize_terminal(*size)
                    app.refresh_state()
                    app.render_progress()
                    await pilot.pause()
                    svg = app.export_screenshot()
                    text = screenshot_text(svg)
                    if renders:
                        (renders / f'scientific-{scenario}-{size[0]}x{size[1]}.svg').write_text(svg)
                    card = str(app.query_one('#progress', Static).render())
                    geometry = {name: (app.query_one('#' + name).region, str(app.query_one('#' + name).render()))
                                for name in ('controller', 'table', 'guard', 'external-heading', 'selected', 'execution',
                                             'progress', 'message', 'execution-controls', 'shortcuts')}
                    assert table.size.height - table.header_height >= 3, (scenario, size, 'fewer than three useful job rows', geometry)
                    footer = app.query_one(Footer) if app.query_one(Footer).display else app.query_one('#shortcuts')
                    assert footer.region.height > 0 and footer.region.bottom <= size[1], (scenario, size, 'footer clipped', geometry)
                    assert app.query_one('#execution-controls').region.bottom <= size[1], (scenario, size, 'suspension controls clipped')
                    assert app.query_one('#progress').region.bottom <= app.query_one('#command-bar').region.y, (scenario, size, 'scientific card clipped', geometry)
                    assert app.query_one('#progress').size.height >= len(card.splitlines()), (scenario, size, 'scientific rows clipped', geometry)
                    assert all(word in text for word in ('SCF', 'FORCE', 'ELECTRONIC', '791%', 'RSS')), (scenario, size)
                    assert all(word in card for word in ('OUTCAR SCF 9/300', 'EDIFF 1E-5 eV'))
                    if scenario == 'force-decimal':
                        assert '0.014864 eV/Å @E26' in card and 'Target 0.020 eV/Å' in card, (scenario, size, card)
                        assert '1.4864e-02' not in card
                    else:
                        assert 'static' in card
                    if scenario != 'missing-metrics':
                        assert all(word in card for word in ('|dE|', '|d eps|', 'rms', '1.5254e-04 eV @SCF8',
                            '6.9071e-07 eV @SCF8', '4.15e-04 @SCF8', 'data')), (scenario, size, card)
                    else:
                        assert all(word in card for word in ('|dE|', '|d eps|', 'rms', '—'))
                        assert not any(value in card for value in ('1.5254e-04', '6.9071e-07', '4.15e-04'))
                    framed = True
                    assert '┌─' in card, (scenario, size, 'borderless scientific card', geometry)
                    assert app.query_one('#progress').has_class('framed')
                    assert app.query_one('#progress').styles.padding.left == 0
                    assert app.query_one('#progress').styles.border_left[0] == ''
                    assert app.query_one('#selected').styles.border_left[0] == 'solid'
                    panel = card.splitlines()[next(i for i, line in enumerate(card.splitlines()) if line.startswith('┌─')):]
                    assert len({cell_width(line) for line in panel}) == 1, (scenario, size, panel)
                    assert panel[-1].startswith('└') and panel[-1].endswith('┘'), (scenario, size, panel)
                    boundaries = [tuple(cell_width(line[:i]) for i, char in enumerate(line) if char == '│') for line in panel[1:5]]
                    assert len(set(boundaries)) == 1 and len(boundaries[0]) == 4, (scenario, size, boundaries)
                    if scenario == 'unverified-execution':
                        assert 'UNVERIFIED' in text and 'Reconciliation required' in text, (scenario, size)
                    else:
                        assert 'Execution scope: worker + anchor' in text, (scenario, size, 'execution scope hidden')
                        assert 'Verified worker + anchor' in text, (scenario, size, 'execution observation hidden')
                    if scenario == 'suspended':
                        assert 'inactivity expected' in text and 'Continue job' in text, (scenario, size)
                    if scenario == 'guard-warning':
                        assert 'UNCERTAIN' in text or 'uncertain' in text, (scenario, size)
                    if scenario == 'status-error':
                        assert 'Readiness unavailable' in text, (scenario, size)
                    if scenario == 'provisional':
                        assert 'provisional' in card, (scenario, size)
                    region = app.query_one('#progress').region
                    app.render_progress()
                    await pilot.pause()
                    assert str(app.query_one('#progress').render()) == card
                    assert app.query_one('#progress').region == region
                    assert '(native' not in card and 'allowed' not in card
                    if scenario == 'conflict':
                        assert 'CONFLICT' in text and 'ERROR' in text
                    elif scenario in ('partial', 'cached-error'):
                        assert 'outcar checked 6' in text and '8 source-local' in text, (scenario, size, card)
                    if scenario == 'cached-error':
                        assert all(word in text for word in ('cached', 'ERROR', 'native warnings', 'settings differ', '8 source-local')), (scenario, size, card)
                    if scenario == 'matched' and size == (80, 24):
                        message = app.query_one('#message', Static)
                        previous_message = message.render()
                        message.update('Checking declared file locations…')
                        app.render_progress()
                        await pilot.pause()
                        assert str(app.query_one('#progress').render()) == card
                        assert app.query_one('#progress').region == region
                        message.update(previous_message)
                        with patch.object(store, 'snapshot', side_effect=OSError(
                                'Synthetic Jobs state read failed; retry read-only observation after checking Details.')):
                            app.refresh_state()
                            await pilot.pause()
                            assert 'State unavailable' in str(message.render())
                            assert '┌─' in str(app.query_one('#progress').render())
                            assert table.size.height - table.header_height >= 3
                            assert app.query_one('#progress').region.bottom <= app.query_one('#command-bar').region.y
                            assert app.query_one('#shortcuts').region.bottom <= size[1]
                        message.update(previous_message)
                        app.refresh_state()
                        await pilot.pause()
                        assert str(app.query_one('#progress').render()) == card
                        assert app.query_one('#progress').region == region
                    if scenario == 'matched' and size == (140, 40):
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
                        assert widget.has_class('framed')
                        assert 'settings differ' in str(widget.render())
                        assert str(widget.render()) != card
                        app.progress_snapshot = snapshot
                        app.render_progress()
                        await pilot.pause()
                    assert snapshot.to_dict() == evidence_before
                    assert app.selected_id == selected['display_id']
                    views.append(dict(scenario=scenario, size=size, useful_rows=table.size.height-table.header_height,
                                      card=card, frame_rows=len(panel), progress_height=app.query_one('#progress').size.height,
                                      footer_bottom=footer.region.bottom))
            await pilot.press('q')
    assert store.snapshot(now=NOW) == baseline, 'Synthetic state changed during observation'
    return views


@unittest.skipIf(JobsApp is None, 'Optional Jobs console not installed')
class ScientificCardUiTests(unittest.TestCase):
    def test_synthetic_source_verification_cards_at_required_and_narrow_sizes(self):
        with tempfile.TemporaryDirectory(prefix='cmw-scientific-card-ui-') as directory:
            views = asyncio.run(scientific_card_views(Path(directory)))
        self.assertEqual(len(views), 14 * len(SIZES))
        self.assertTrue(all(view['useful_rows'] >= 3 for view in views))


if __name__ == '__main__':
    unittest.main()
