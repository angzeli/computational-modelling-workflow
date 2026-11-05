"""Compact, read-only study observations reached from the existing catalogue."""
from __future__ import annotations

from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from .copy_ui import SelectableStatic
from .snapshot import safe_snapshot_text


from .study_presentation import study_text


class StudyScreen(ModalScreen):
    BINDINGS = [Binding('escape', 'dismiss(None)', 'Back', priority=True),
                Binding('q', 'dismiss(None)', 'Back'),
                Binding('ctrl+c', 'dismiss(None)', 'Back', priority=True)]
    DEFAULT_CSS = '''
    StudyScreen { align: center middle; background: $background 70%; }
    StudyScreen > Vertical { width: 96%; max-width: 138; height: 94%; border: round $accent; padding: 0 1; background: $surface; }
    StudyScreen #study-title { height: 1; }
    StudyScreen #study-scroll { height: 1fr; min-height: 4; }
    StudyScreen #study-body { height: auto; }
    StudyScreen #study-status { height: auto; max-height: 3; }
    StudyScreen #study-actions { height: 3; }
    StudyScreen Button { width: 1fr; }
    '''

    def __init__(self, label, request, owner=None):
        super().__init__()
        self.label, self.request, self.record = label, request, None
        self.owner = owner

    def compose(self):
        with Vertical():
            yield Label(safe_snapshot_text('STUDY SERIES · '+self.label), id='study-title')
            with VerticalScroll(id='study-scroll'):
                yield SelectableStatic('Loading bounded owner evidence…', id='study-body', markup=False)
            yield Static('Read-only observation. No scientific inspection or submission.', id='study-status', markup=False)
            with Horizontal(id='study-actions'):
                yield Button('Back', id='study-back')

    def on_mount(self):
        def accepted(record, error, receipt=None):
            if not self.is_mounted:
                return
            if error is not None:
                self.query_one('#study-status', Static).update(safe_snapshot_text('Study unavailable / incomplete owner coverage: '+str(error)))
                self.query_one('#study-body', Static).update('No fresh series evidence. No comparison or convergence verdict is available.')
                return
            self.record = record
            if self.owner is not None and receipt is not None:
                self.app.view_received(self, self.owner, receipt)
            self.query_one('#study-body', Static).update(study_text(record))
            self.query_one('#study-status', Static).update('Recorded study bindings only; use explicit study compare for fresh scientific qualification.')
        self.request(accepted)
        self.query_one('#study-scroll').focus()

    def on_button_pressed(self, event):
        if event.button.id == 'study-back':
            event.stop()
            self.dismiss(None)
