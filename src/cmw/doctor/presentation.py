"""Optional Jobs UI routes to frozen, read-only runtime diagnostics."""
from __future__ import annotations

from textual.binding import Binding
from textual.widgets import Static, TextArea

from cmw.jobs.copy_ui import FrozenText
from cmw.jobs.snapshot import safe_snapshot_text


class DoctorDetails(FrozenText):
    """Keep the existing Details view and its captured target when opening Doctor."""

    BINDINGS = [Binding('d', 'doctor', 'Doctor', priority=True)]

    def __init__(self, text, title, selection, doctor):
        super().__init__(text, title)
        self.selection, self.open_doctor = selection, doctor
        self.commands_allowed = True

    def on_mount(self):
        super().on_mount()
        self.query_one('#copy-status', Static).update(
            'D: Doctor · Select text, then Ctrl-C. No selection: back. Esc: back.')

    def node_commands_changed(self, allowed):
        self.commands_allowed = allowed

    def check_action(self, action, parameters):
        if action == 'doctor':
            return self.commands_allowed
        return super().check_action(action, parameters)

    def action_doctor(self):
        if self.commands_allowed:
            self.open_doctor()


class DoctorReport(FrozenText):
    """One inspection per open view; slow collection never blocks navigation."""

    def __init__(self, selection, label, *, loader=None, context=''):
        self.selection, self.label, self.context = selection, label, context
        self.on_report_displayed = None
        self.loader = loader
        self.report = None
        self.pending = True
        self.started = False
        super().__init__(self.header() + '\n\nInspecting captured target…',
                         'RUNTIME / CAPABILITY DOCTOR · read only')

    def header(self):
        return self.label + ('\n' + self.context if self.context else '')

    def on_mount(self):
        super().on_mount()
        self.query_one('#copy-status', Static).update(
            'Read-only snapshot. Review paths before sharing. Esc: back.')
        if self.loader is not None:
            self.call_after_refresh(self.load, self.loader)

    def load(self, loader, *, on_displayed=None):
        if self.started:
            return
        if on_displayed is not None:
            self.on_report_displayed = on_displayed
        if not self.is_mounted:
            self.loader = loader  # A fast endpoint reply can precede Mount.
            return
        if self not in self.app.screen_stack:
            return
        self.started = True
        app = self.app

        def collect():
            try:
                from cmw.doctor.service import format_report
                report = loader()
                text = format_report(report)
                error = None
            except Exception as exc:
                report, text, error = None, None, str(exc)
            try:
                app.call_from_thread(self.accept, report, text, error)
            except RuntimeError:
                pass  # Detaching never starts another inspection or a repair.

        self.run_worker(collect, thread=True, exit_on_error=False)

    def accept(self, report, text, error=None):
        if not self.is_mounted or self not in self.app.screen_stack:
            return
        self.pending = False
        self.report = report
        body = ('Doctor unavailable: ' + error) if error else text
        self.query_one(TextArea).load_text(safe_snapshot_text(self.header() + '\n\n' + body))

        if report is not None and self.on_report_displayed is not None:
            self.on_report_displayed()

    def unavailable(self, reason):
        self.accept(None, None, str(reason))
