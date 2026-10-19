"""Selectable, frozen observation previews; no queue or filesystem mutation."""
from __future__ import annotations

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Label, Select, Static, TextArea

from .snapshot import render_snapshot, safe_snapshot_text


class SelectableStatic(Static):
    """Native Textual selection where available; postpone paints during selection."""
    ALLOW_SELECT = True

    def update(self, content='', *, layout=True):
        if getattr(self, '_copy_selected', False):
            self._copy_pending = (content, layout)
            return
        super().update(content, layout=layout)

    def selection_updated(self, selection):
        selected = self.get_selection(selection) if selection is not None else None
        self._copy_selected = bool(selected and selected[0])
        parent = getattr(super(), 'selection_updated', None)
        if parent is not None:
            parent(selection)
        if not self._copy_selected and getattr(self, '_copy_pending', None) is not None:
            content, layout = self._copy_pending
            self._copy_pending = None
            self.update(content, layout=layout)


class FrozenText(ModalScreen):
    BINDINGS = [Binding('escape', 'dismiss(None)', 'Back', priority=True),
                Binding('ctrl+c', 'dismiss(None)', 'Back', priority=True)]
    DEFAULT_CSS = '''
    FrozenText { align: center middle; background: $background 70%; }
    FrozenText > Vertical { width: 96%; height: 94%; border: round $accent; padding: 0 1; background: $surface; }
    FrozenText TextArea { height: 1fr; }
    FrozenText #copy-status { height: auto; max-height: 3; color: $warning; }
    FrozenText Button { height: 3; }
    '''

    def __init__(self, text, title='FROZEN TEXT · read only'):
        super().__init__()
        self.text, self.title_text = safe_snapshot_text(text), safe_snapshot_text(title)

    def compose(self):
        with Vertical():
            yield Label(self.title_text)
            yield TextArea(self.text, read_only=True, id='copy-text')
            yield Static('Select text, then Ctrl-C. No selection: back. Esc: back.', id='copy-status', markup=False)
            yield Button('Copy all', id='copy-all')

    def on_mount(self):
        self.query_one(TextArea).focus()

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'copy-all':
            self.app.request_copy(self.query_one(TextArea).text, view=self)


class CopyPreview(FrozenText):
    """A capture stays fixed until explicit refresh of its original attempt."""
    DEFAULT_CSS = FrozenText.DEFAULT_CSS.replace('FrozenText', 'CopyPreview') + '''
    CopyPreview #copy-options { height: auto; }
    CopyPreview Select { width: 24; }
    CopyPreview Checkbox { width: 1fr; }
    CopyPreview #copy-buttons { height: 3; }
    CopyPreview Button { min-width: 12; margin-right: 1; }
    '''

    def __init__(self, snapshot, refresh=None):
        self.snapshot, self.refresh_snapshot = snapshot, refresh
        job = snapshot.to_dict()['job']
        self.identity = (job['id'], job['attempt_id'])
        self.mode = 'summary'
        self.redact_paths = False
        super().__init__(render_snapshot(snapshot), 'COPY SNAPSHOT · captured attempt · read only')

    def compose(self):
        with Vertical():
            yield Label(self.title_text)
            with Horizontal(id='copy-options'):
                yield Select([('Summary', 'summary'), ('Diagnostics', 'diagnostics'), ('JSON', 'json')],
                             value='summary', allow_blank=False, id='copy-format')
                yield Checkbox('Redact paths', id='copy-redact')
            yield TextArea(self.text, read_only=True, id='copy-text')
            yield Static('Frozen values. Diagnostics/JSON may contain sensitive paths; review or redact before sharing.',
                         id='copy-status', markup=False)
            with Horizontal(id='copy-buttons'):
                yield Button('Copy all', id='copy-all', variant='primary')
                yield Button('Refresh', id='copy-refresh')
                yield Button('Back', id='copy-back')

    def render_capture(self):
        self.text = render_snapshot(self.snapshot, mode=self.mode, redact_paths=self.redact_paths)
        self.query_one(TextArea).load_text(self.text)

    def on_select_changed(self, event):
        if event.select.id == 'copy-format' and isinstance(event.value, str):
            self.mode = event.value
            self.render_capture()

    def on_checkbox_changed(self, event):
        if event.checkbox.id == 'copy-redact':
            self.redact_paths = event.value
            self.render_capture()

    def on_button_pressed(self, event):
        event.stop()
        event.prevent_default()
        if event.button.id == 'copy-all':
            self.app.request_copy(self.query_one(TextArea).text, view=self)
        elif event.button.id == 'copy-back':
            self.dismiss(None)
        elif event.button.id == 'copy-refresh':
            capture = self.refresh_snapshot(self.identity) if self.refresh_snapshot else None
            if capture is None:
                self.query_one('#copy-status', Static).update('Captured attempt unavailable or selection/binding changed; original snapshot retained.')
                return
            self.snapshot = capture
            self.render_capture()
            self.query_one('#copy-status', Static).update('Explicitly refreshed from the captured attempt’s current client cache.')
