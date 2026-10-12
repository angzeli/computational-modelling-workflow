"""Explicit selected-attempt controls; the detached worker owns all signalling."""
from __future__ import annotations

from copy import deepcopy

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from .cli import safe_text
from .suspension_presentation import readiness_text


class SuspensionConfirm(ModalScreen):
    BINDINGS = [Binding('escape', 'dismiss(None)', 'Back', priority=True),
                Binding('ctrl+c', 'detach', 'Detach', priority=True)]
    DEFAULT_CSS = '''
    SuspensionConfirm { align: center middle; background: $background 70%; }
    SuspensionConfirm > Vertical { width: 94%; height: 92%; padding: 0 1; border: round $accent; background: $surface; }
    SuspensionConfirm VerticalScroll { height: 1fr; min-height: 4; }
    SuspensionConfirm #suspension-readiness { height: auto; }
    SuspensionConfirm #suspension-reason { height: 3; }
    SuspensionConfirm #suspension-error { height: auto; max-height: 3; color: $warning; }
    SuspensionConfirm Horizontal { height: 3; }
    SuspensionConfirm Button { min-width: 12; margin-right: 1; }
    SuspensionConfirm #suspension-help { height: auto; }
    '''

    def __init__(self, request, result):
        super().__init__()
        self.request, self.result = request, deepcopy(result)
        self.busy = False

    def compose(self):
        with Vertical():
            yield Label('EXECUTION CONTROL · explicit original-attempt request')
            with VerticalScroll():
                yield Static(Text(safe_text(readiness_text(self.result))), id='suspension-readiness')
            yield Input(placeholder='Optional literal reason', id='suspension-reason')
            yield Static('', id='suspension-error', markup=False)
            with Horizontal():
                yield Button(self.request['action'].title() + ' job', id='suspension-confirm',
                             variant='primary', disabled=not self.result.get('ready', False))
                yield Button('Back', id='suspension-back')
            yield Label('Confirm submits a request; worker verification completes it.\nEsc Back · Ctrl-C Detach · letters in the reason remain text', id='suspension-help')

    def on_mount(self):
        self.query_one(Input).focus()

    def action_detach(self):
        self.app.exit()

    def on_input_submitted(self, event):
        # Enter in a reason is not an execution confirmation.
        event.stop()
        self.query_one('#suspension-confirm', Button).focus()
        self.message('Reason retained. Press Enter on the selected button to confirm.')

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'suspension-back':
            self.dismiss(None)
        elif event.button.id == 'suspension-confirm' and not self.busy:
            self.app.submit_suspension(self, self.query_one(Input).value)

    def message(self, message):
        self.query_one('#suspension-error', Static).update(Text(safe_text(message)))
