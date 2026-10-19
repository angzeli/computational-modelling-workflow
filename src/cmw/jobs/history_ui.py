"""Generic, read-only history navigation over the shared observation client."""
from __future__ import annotations

from dataclasses import replace

from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label, Static

from .copy_ui import FrozenText, SelectableStatic

from cmw.progress.formatting import (context_text, history_cell, history_columns,
                                     history_entry_text)


def literal(value):
    from rich.text import Text
    from .cli import safe_text
    return Text(safe_text(value))


class HistoryDetails(ModalScreen):
    BINDINGS = [('y', 'select_text', 'Select frozen text'), ('escape', 'dismiss(None)', 'Back'), ('q', 'dismiss(None)', 'Back'),
                Binding('ctrl+c', 'detach', 'Detach', priority=True)]
    DEFAULT_CSS = '''
    HistoryDetails { align: center middle; background: $background 70%; }
    HistoryDetails > Vertical { width: 96%; height: 94%; border: round $accent; padding: 1; background: $surface; }
    HistoryDetails VerticalScroll { height: 1fr; }
    HistoryDetails Static { height: auto; }
    '''

    def __init__(self, history, entry):
        super().__init__()
        self.history, self.entry_id, self.entry = history, entry.entry_id, entry
        self.source_revision = history.page.source_revision

    def compose(self):
        with Vertical():
            yield Label('PROGRESS ENTRY · metrics / criteria / timing / sources')
            with VerticalScroll(id='history-detail-scroll'):
                yield SelectableStatic(id='history-detail', markup=False)
            yield Label('Esc / Q: history · Y: frozen selectable text · Ctrl-C: detach')

    def action_detach(self):
        self.app.exit()

    def on_mount(self):
        self.refresh_content()
        self.set_interval(.5, self.refresh_content)

    def action_select_text(self):
        self.app.push_screen(FrozenText(str(self.query_one('#history-detail', Static).render())))

    def refresh_content(self):
        if getattr(self.query_one('#history-detail', Static), '_copy_selected', False):
            return
        entry = self.history.entries.get(self.entry_id)
        if entry is not None:
            self.entry = entry
        body = history_entry_text(self.entry)
        if self.history.expired or self.history.page.source_revision != self.source_revision:
            body = 'Source binding changed; this entry is earlier cached evidence.\n\n' + body
        widget = self.query_one('#history-detail', Static)
        if str(widget.render()) != body:
            scroll = self.query_one('#history-detail-scroll', VerticalScroll)
            position = scroll.scroll_offset
            widget.update(literal(body))
            scroll.scroll_to(x=position.x, y=position.y, animate=False, force=True)


class ProgressHistory(ModalScreen):
    """Descriptors and parent keys support any nesting depth, without engine rules."""
    BINDINGS = [Binding('escape', 'dismiss(None)', 'Back'), Binding('q', 'dismiss(None)', 'Back'),
                Binding('enter', 'drill', 'Children'), Binding('d', 'details', 'Details'),
                Binding('backspace', 'parent', 'Parent'), Binding('left_square_bracket', 'previous', 'Older'),
                Binding('right_square_bracket', 'next', 'Newer'), Binding('s', 'stage', 'Stage'),
                Binding('v', 'segment', 'Segment'), Binding('ctrl+c', 'detach', 'Detach', priority=True)]
    DEFAULT_CSS = '''
    ProgressHistory { align: center middle; background: $background 70%; }
    ProgressHistory > Vertical { width: 98%; height: 96%; border: round $accent; padding: 0 1; background: $surface; }
    ProgressHistory #history-title { height: 1; text-style: bold; }
    ProgressHistory #history-scope { height: auto; max-height: 2; }
    ProgressHistory #history-table { height: 1fr; min-height: 4; }
    ProgressHistory #history-state { height: auto; max-height: 4; color: $warning; }
    ProgressHistory #history-selected { height: auto; max-height: 3; }
    ProgressHistory #history-help { height: auto; }
    '''

    def __init__(self, selection, display_id):
        super().__init__()
        self.selection, self.display_id = selection, display_id
        self.context = None
        self.request_generation = 0
        self.cursor = self.parent_id = self.stage = self.segment = None
        self.parents = []
        self.parent_label = 'root contexts'
        self.display_query = None
        self.restore_entry_id = None
        self.page = None
        self.entries = {}
        self.expired = False
        self.columns = ()
        self.closed = False
        self.boundaries = ()

    def compose(self):
        with Vertical():
            yield Static('PROGRESS HISTORY · ' + self.display_id, id='history-title', markup=False)
            yield Static('All stages · all segments · root contexts', id='history-scope', markup=False)
            yield DataTable(id='history-table', cursor_type='row', zebra_stripes=True)
            yield Static('Loading bounded native observations…', id='history-state', markup=False)
            yield Static(id='history-selected', markup=False)
            yield Static('Enter Children · D Details · Backspace Parent\n[ Older · ] Newer · S Stage · V Segment · Esc Back\nCtrl-C Detach', id='history-help', markup=False)

    def action_detach(self):
        self.app.exit()

    def on_mount(self):
        self.app.history_view = self
        self.query_one(DataTable).focus()
        self.request()

    def on_unmount(self):
        self.closed = True
        self.request_generation += 1
        if self.app.history_view is self:
            self.app.history_view = None
        if self.context is not None and self.app.progress_service is not None:
            self.app.progress_service.release_history(self.context)

    def request(self):
        if self.expired or self.closed:
            return
        self.request_generation += 1
        self.app.collect_progress()

    def parameters(self):
        return dict(cursor=self.cursor, limit=50, parent_id=self.parent_id,
                    stage=self.stage, segment=self.segment)

    def bind_context(self, context):
        self.context = context
        if self.closed:
            self.app.progress_service.release_history(context)

    def invalidate(self):
        self.expired = True
        self.request_generation += 1
        self.query_one('#history-state', Static).update('Selected attempt or source declarations changed. Reopen history for the current binding.')
        if self.context is not None and self.app.progress_service is not None:
            self.app.progress_service.release_history(self.context)

    def accept_page(self, context, page, generation):
        if self.closed or self.expired or generation != self.request_generation:
            return
        if self.page is not None and self.page.source_revision and page.source_revision and self.page.source_revision != page.source_revision:
            # Retained rows and selected IDs must never migrate to a new source generation.
            self.entries = {}
            self.parents = []
            self.boundaries = ()
            self.request_generation += 1
            self.parent_id = self.cursor = None
            self.parent_label = 'root contexts'
            self.query_one('#history-table', DataTable).clear()
            page = replace(page, status='LOADING', messages=('Source generation changed; reconstructing the new root contexts.',))
        self.context, self.page = context, page
        self.boundaries = tuple(dict.fromkeys((*self.boundaries, *page.boundaries)))[-64:]
        self.cursor = page.cursor or self.cursor
        table = self.query_one('#history-table', DataTable)
        selected = table.ordered_rows[table.cursor_row].key.value if table.row_count else None
        position = table.scroll_offset
        columns = history_columns(page)
        query = (self.cursor, self.parent_id, self.stage, self.segment)
        if columns != self.columns or query != self.display_query:
            table.clear(columns=True)
            for column in columns:
                table.add_column(column.label, key=column.key)
            self.columns = columns
            self.display_query = query
        keys = [entry.entry_id for entry in page.entries]
        old_keys = [row.key.value for row in table.ordered_rows]
        if old_keys != keys:
            table.clear()
            for entry in page.entries:
                table.add_row(*(literal(history_cell(entry, column)) for column in columns), key=entry.entry_id)
        else:
            for entry in page.entries:
                for column in columns:
                    table.update_cell(entry.entry_id, column.key, literal(history_cell(entry, column)))
        self.entries = {entry.entry_id: entry for entry in page.entries}
        if self.restore_entry_id in self.entries:
            selected = self.restore_entry_id
            self.restore_entry_id = None
        if selected in self.entries:
            table.move_cursor(row=keys.index(selected), animate=False, scroll=False)
        table.scroll_to(x=position.x, y=position.y, animate=False, force=True)
        parent = self.parent_label
        self.query_one('#history-scope', Static).update(literal(
            f'Stage: {self.stage or "all"} · Segment: {self.segment or "all"} · {parent}'))
        total = 'unknown' if page.total is None else str(page.total)
        message = (f'{page.status} · {page.coverage} · {len(page.entries)} shown / {total} total\n'
                   + ('Older [ available · ' if page.previous_cursor else 'Start of selected scope · ')
                   + ('Newer ] available' if page.next_cursor else 'No newer page yet'))
        if page.messages:
            message += '\n' + page.messages[0]
        if not page.entries and page.status != 'LOADING':
            message += '\nNo observed entries in this context. Queued jobs may have no native output yet.'
        self.query_one('#history-state', Static).update(literal(message))
        self.refresh_selected()

    def refresh_selected(self):
        entry = self.selected_entry()
        self.query_one('#history-selected', Static).update(literal(
            f'{entry.kind.replace("-", " ")} · {entry.state} · {context_text(entry.context) if entry.context else entry.native_label}\n{entry.coverage}; D shows source and timing qualifications.' if entry else 'No entry selected.'))

    def selected_entry(self):
        table = self.query_one('#history-table', DataTable)
        return self.entries.get(table.ordered_rows[table.cursor_row].key.value) if table.row_count else None

    def on_data_table_row_highlighted(self, event):
        event.stop()
        self.refresh_selected()

    def on_data_table_row_selected(self, event):
        event.stop()
        self.action_drill()

    def action_drill(self):
        entry = self.selected_entry()
        if entry is None:
            return
        if not entry.child_count:
            self.action_details()
            return
        self.parents.append((self.parent_id, self.cursor, self.parent_label, entry.entry_id))
        self.parent_id, self.cursor = entry.entry_id, None
        self.parent_label = context_text(entry.context, budget=False)
        self.request()

    def action_details(self):
        entry = self.selected_entry()
        if entry is not None:
            self.app.push_screen(HistoryDetails(self, entry))

    def action_parent(self):
        if self.parents:
            self.parent_id, self.cursor, self.parent_label, self.restore_entry_id = self.parents.pop()
            self.request()

    def action_previous(self):
        if self.page and self.page.previous_cursor:
            self.cursor = self.page.previous_cursor
            self.request()

    def action_next(self):
        if self.page and self.page.next_cursor:
            self.cursor = self.page.next_cursor
            self.request()

    def cycle_scope(self, name, values):
        options = [None, *dict.fromkeys(value for value in values if value is not None)]
        old = getattr(self, name)
        setattr(self, name, options[(options.index(old) + 1) % len(options)] if old in options else None)
        self.parent_id = self.cursor = None
        self.parent_label = 'root contexts'
        self.parents = []
        self.request()

    def action_stage(self):
        if self.page:
            self.cycle_scope('stage', (boundary[0] for boundary in self.boundaries))

    def action_segment(self):
        if self.page:
            self.cycle_scope('segment', (boundary[1] for boundary in self.boundaries
                                      if self.stage is None or boundary[0] == self.stage))
