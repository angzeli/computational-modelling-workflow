"""Complete event/geometry counterexamples for the navigation regression tests.

This observer reads widgets directly; it does not call capture_view/restore_view
as an oracle or suppress Textual watchers, backend polling, or native input.
"""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch


def screen_state(screen):
    return {'object': id(screen), 'class': type(screen).__name__,
            'mounted': screen.is_mounted, 'attached': screen.is_attached,
            'size': list(screen.size), 'layout_pending': bool(screen._layout_required)}


class NavigationTrace:
    def __init__(self, app, name):
        self.app, self.name = app, name
        self.events = []
        self.render_generation = self.layout_generation = 0
        self.layout = None
        self.patches = ExitStack()

    def snapshot(self):
        from textual.widgets import DataTable
        app = self.app
        origin = app.screen_stack[0]
        selected = app.selected()
        job = selected[1] if isinstance(selected, tuple) else selected
        tables = []
        for table in origin.query(DataTable):
            rows = table.ordered_rows
            top = int(table.scroll_y)
            height = table.scrollable_content_region.height - table.header_height
            ancestors = []
            ancestor = table.parent
            while ancestor is not None:
                if hasattr(ancestor, 'scroll_x'):
                    ancestors.append({'object': id(ancestor), 'id': ancestor.id,
                                      'scroll': [ancestor.scroll_x, ancestor.scroll_y],
                                      'targets': [ancestor.scroll_target_x, ancestor.scroll_target_y]})
                ancestor = ancestor.parent
            tables.append({'id': table.id, 'row_keys': [row.key.value for row in rows],
                'cursor': rows[table.cursor_row].key.value if rows else None,
                'cursor_row': table.cursor_row, 'column': table.cursor_column,
                'visible': [row.key.value for row in rows[top:top+height]],
                'anchor': rows[top].key.value if 0 <= top < len(rows) else None,
                'relative': table.cursor_row-table.scroll_y,
                'scroll': [table.scroll_x, table.scroll_y],
                'targets': [table.scroll_target_x, table.scroll_target_y],
                'max_scroll': [table.max_scroll_x, table.max_scroll_y],
                'region': list(table.region), 'content_region': list(table.scrollable_content_region),
                'ancestors': ancestors})
        card = origin.query_one('#progress')
        geometry = [list(card.region), *[row['region'] for row in tables]]
        if geometry != self.layout:
            self.layout_generation += 1
            self.layout = geometry
        responses = {}
        for owner, lane in getattr(app, 'lanes', {}).items():
            responses[owner] = {'received': lane.received, 'session': lane.session,
                'progress': [{'target': key, 'sequence': cache['progress'][0]['sequence'],
                              'received': cache['progress'][1]}
                             for key, cache in lane.cache.items() if 'progress' in cache]}
        return {'target': deepcopy(job.get('target', {'job_id': job['id'], 'attempt_id': job['attempt_id']})) if job else None,
            'selection': getattr(app, 'selected_key', None),
            'selection_generation': getattr(app, 'selection_generation', None),
            'input_generation': app._navigation_inputs.get(origin, 0),
            'origin': screen_state(origin), 'current': screen_state(app.screen),
            'stack': [screen_state(screen) for screen in app.screen_stack],
            'tables': tables, 'card_region': list(card.region),
            'card_text': str(card.render()), 'render_generation': self.render_generation,
            'layout_generation': self.layout_generation,
            'callbacks': [{'object': id(restore), 'screen': id(restore.view.screen),
                           'input_generation': restore.view.input_generation,
                           'selection': restore.view.selection, 'remaining_tables': restore.remaining_tables}
                          for restore in app._navigation_restores.values()],
            'scientific_responses': responses}

    def mark(self, event, **detail):
        self.events.append({'event': event, 'at': time.monotonic(), **detail,
                            'state': self.snapshot()})

    def __enter__(self):
        app = self.app
        for name in ('restore_view', '_restore_cursor', '_cursor_drained', '_restore_offsets', '_discard_restore', 'accept'):
            original = getattr(app, name)
            def traced(*args, _name=name, _original=original, **kwargs):
                callback = args[0] if args else None
                detail = {'callback': id(callback)} if _name != 'accept' else {'owner': args[0], 'request': repr(args[1])}
                if _name.startswith('_') and callback is not None:
                    detail['valid'] = app._restore_valid(callback)
                self.mark(_name+':before', **detail)
                result = _original(*args, **kwargs)
                self.mark(_name+':after', **detail)
                return result
            self.patches.enter_context(patch.object(app, name, side_effect=traced))
        original_render = app.render_state
        def render():
            self.render_generation += 1
            return original_render()
        self.patches.enter_context(patch.object(app, 'render_state', side_effect=render))
        original_event = app.on_event
        async def event(message):
            from textual import events
            tracked = isinstance(message, (events.Key, events.MouseDown, events.MouseUp,
                                            events.MouseScrollUp, events.MouseScrollDown, events.Resize))
            detail = {'type': type(message).__name__, 'key': getattr(message, 'key', None),
                      'forwarded': message.is_forwarded}
            if tracked:
                self.mark('input:before', **detail)
            await original_event(message)
            if tracked:
                self.mark('input:after', **detail)
        self.patches.enter_context(patch.object(app, 'on_event', side_effect=event))
        self.mark('trace:start')
        return self

    def __exit__(self, kind, error, traceback):
        self.mark('trace:failure' if error else 'trace:complete', error=repr(error) if error else None)
        self.patches.close()
        destination = os.environ.get('CMW_NAVIGATION_TRACE_DIR')
        if destination or error:
            root = Path(destination) if destination else Path(tempfile.mkdtemp(prefix='cmw-navigation-failure-'))
            root.mkdir(parents=True, exist_ok=True)
            path = root/(self.name+'.json')
            path.write_text(json.dumps({'schema': 1, 'test': self.name, 'events': self.events}, indent=2, default=str)+'\n')
            if error is not None and hasattr(error, 'add_note'):
                error.add_note('Full navigation counterexample: '+str(path))
        return False
