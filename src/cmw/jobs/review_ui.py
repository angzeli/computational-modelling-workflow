"""Selected-attempt result review; inspection and export require explicit actions."""
from __future__ import annotations

from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from .copy_ui import FrozenText, SelectableStatic
from .review_models import review_text
from .snapshot import safe_snapshot_text


class ReviewScreen(ModalScreen):
    BINDINGS = [Binding('i','inspect','Inspect'), Binding('c','cancel_review','Cancel observer'),
                Binding('y','copy_review','Frozen text'), Binding('q','dismiss(None)','Back'),
                Binding('ctrl+c','dismiss(None)','Back',priority=True), Binding('escape','dismiss(None)','Back',priority=True)]
    DEFAULT_CSS = '''
    ReviewScreen { align: center middle; background: $background 70%; }
    ReviewScreen > Vertical { width: 96%; max-width: 138; height: 94%; border: round $accent; padding: 0 1; background: $surface; }
    ReviewScreen #review-title { height: 1; }
    ReviewScreen #review-scroll { height: 1fr; min-height: 4; }
    ReviewScreen #review-body { height: auto; }
    ReviewScreen #review-status { height: auto; max-height: 2; color: $warning; }
    ReviewScreen Input { height: 1; border: none; margin: 0; padding: 0 1; }
    ReviewScreen #review-limits { height: 1; }
    ReviewScreen #review-limits Label { width: auto; }
    ReviewScreen #review-bytes { width: 18; }
    ReviewScreen #review-seconds { width: 8; }
    ReviewScreen #review-actions { height: 3; }
    ReviewScreen Button { min-width: 12; width: 1fr; }
    ReviewScreen #review-help { height: auto; max-height: 2; }
    '''

    def __init__(self, label, request, *, cached=None):
        super().__init__()
        self.label, self.request, self.record = label, request, cached
        self.busy = False
        self.unavailable = False
        self.commands_allowed = True

    def compose(self):
        with Vertical():
            yield Label(safe_snapshot_text('RESULT REVIEW · '+self.label), id='review-title')
            with VerticalScroll(id='review-scroll'):
                yield SelectableStatic(id='review-body', markup=False)
            yield Static('Loading owner metadata; no scientific inspection started.', id='review-status', markup=False)
            yield Input(placeholder='Policy: optional absolute path on the owner node', id='review-policy')
            yield Input(placeholder='Binding spec: optional absolute path on the owner node', id='review-spec')
            yield Input(placeholder='Export: optional new absolute path outside calculation inputs', id='review-export')
            with Horizontal(id='review-limits'):
                yield Label('Limit bytes: ')
                yield Input(value=str(64*1024*1024), id='review-bytes', type='integer')
                yield Label(' seconds: ')
                yield Input(value='30', id='review-seconds', type='integer')
            with Horizontal(id='review-actions'):
                yield Button('I Inspect', id='review-inspect', variant='primary')
                yield Button('C Cancel', id='review-cancel')
                yield Button('Y Text', id='review-copy')
                yield Button('Back', id='review-back')
            yield Static('I explicitly reads / hashes; an export path also writes a new review.\nC stops only the observer. Esc returns; finalization stays separate.', id='review-help', markup=False)

    def on_mount(self):
        self.render_record()
        self.query_one('#review-scroll').focus()
        self.refresh_node_observation()
        self.set_interval(.5, self.poll)

    def refresh_node_observation(self):
        # Re-read current owner metadata, including after a session loses its handle.
        self.send('review.preview', {})

    def node_commands_changed(self, allowed):
        self.commands_allowed = allowed
        if self.is_mounted:
            self.render_record()

    def check_action(self, action, parameters):
        if action in {'inspect', 'cancel_review'} and not self.commands_allowed:
            return False
        if isinstance(self.focused, Input) and action in {'inspect','cancel_review','copy_review'}:
            return False
        return True

    def render_record(self):
        text = ('\n'.join(f'{domain.name.replace("_", " ").upper()}: {domain.status}' for domain in self.record.domains)
                + '\n\n' + review_text(self.record)) if self.record is not None else 'No cached review. Owner metadata is being requested.'
        self.query_one('#review-body', Static).update(safe_snapshot_text(text))
        self.query_one('#review-inspect', Button).disabled = not self.commands_allowed or self.busy or bool(self.record and self.record.status == 'PENDING' and not self.unavailable)
        self.query_one('#review-cancel', Button).disabled = not self.commands_allowed or self.busy or not bool(self.record and self.record.review_id and self.record.status == 'PENDING')
        self.query_one('#review-copy', Button).disabled = self.record is None

    def send(self, operation, params):
        if self.busy or (not self.commands_allowed and operation != 'review.preview'):
            return
        self.busy = True
        self.render_record()
        def accepted(record, error):
            if not self.is_mounted:
                return
            self.busy = False
            if error is not None:
                self.unavailable = True
                message = 'Review unavailable; cached observation retained: '+str(error)
            else:
                self.unavailable = False
                self.record = record
                message = record.message
            self.query_one('#review-status', Static).update(safe_snapshot_text(message))
            self.render_record()
        self.request(operation, params, accepted)

    def poll(self):
        if not self.busy and not self.unavailable and self.record is not None and self.record.status == 'PENDING':
            self.send('review.poll', {'review_id':self.record.review_id})

    def action_inspect(self):
        if not self.commands_allowed or self.busy or self.record and self.record.status == 'PENDING' and not self.unavailable:
            return
        try:
            settings = {'max_bytes':int(self.query_one('#review-bytes', Input).value),
                        'timeout_seconds':int(self.query_one('#review-seconds', Input).value)}
            for key, widget in (('policy_path','policy'),('spec_path','spec'),('export_path','export')):
                value = self.query_one('#review-'+widget, Input).value.strip()
                if value:
                    settings[key] = value
            from .review import options
            options(settings)  # Pure option validation; remote paths are never opened here.
        except (ValueError, OSError) as exc:
            self.query_one('#review-status', Static).update(safe_snapshot_text(str(exc)))
            return
        self.send('review.start', {'options':settings})

    def action_cancel_review(self):
        if self.record and self.record.review_id and self.record.status == 'PENDING':
            self.send('review.cancel', {'review_id':self.record.review_id})

    def action_copy_review(self):
        if self.record:
            self.app.push_screen(FrozenText(review_text(self.record), 'RESULT REVIEW · frozen observation'))

    def on_button_pressed(self, event):
        action = {'review-inspect':self.action_inspect, 'review-cancel':self.action_cancel_review,
                  'review-copy':self.action_copy_review, 'review-back':lambda:self.dismiss(None)}.get(event.button.id)
        if action:
            event.stop()
            action()
