"""Inspect-first, attempt-scoped source selection over the shared progress worker."""
from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Static

from cmw.progress.models import SourceDescriptor
from cmw.progress.registry import get_registration
from .cli import safe_text


def literal(value):
    return Text(safe_text(value))


class Sources(ModalScreen):
    BINDINGS = [Binding('escape', 'back', 'Back', priority=True),
                Binding('ctrl+c', 'detach', 'Detach', priority=True)]
    DEFAULT_CSS = '''
    Sources { align: center middle; background: $background 70%; }
    Sources > Vertical { width: 96%; height: 94%; border: round $accent; padding: 0 1; background: $surface; }
    Sources #sources-title { height: 1; text-style: bold; }
    Sources #sources-scroll { height: 1fr; }
    Sources Static { height: auto; }
    Sources #sources-fields { height: auto; }
    Sources Input { margin-bottom: 1; }
    Sources #sources-validation { max-height: 4; color: $warning; }
    Sources #sources-actions { height: 3; }
    Sources Button { min-width: 8; margin-right: 1; }
    Sources #sources-help { height: 1; }
    Sources Checkbox { height: auto; }
    '''

    def __init__(self, selection, display_id):
        super().__init__()
        self.selection, self.display_id = selection, display_id
        self.closed = False
        self.expired = False
        self.busy = False
        self.request_generation = 0
        self.inspection = None
        self.validation = None
        self.validated_fields = None
        self.configuring = False
        self.details = False
        self.plans = ()

    def compose(self):
        with Vertical():
            yield Static('SCIENTIFIC SOURCES · ' + self.display_id, id='sources-title', markup=False)
            with VerticalScroll(id='sources-scroll'):
                yield Static('Inspecting recorded declarations…', id='sources-inspection', markup=False)
                yield Vertical(id='sources-fields')
                yield Checkbox('I reviewed the conflict / override and choose these sources.', id='sources-confirm')
            yield Static('', id='sources-validation', markup=False)
            with Horizontal(id='sources-actions'):
                yield Button('Details', id='sources-details')
                yield Button('Use Different Sources', id='sources-edit')
                yield Button('Use Default', id='sources-clear')
                yield Button('Validate', id='sources-validate')
                yield Button('Use This Session', id='sources-use', variant='primary')
                yield Button('Back', id='sources-back')
            yield Static('Esc: back · Ctrl-C: detach · Paths are literal; no execution.', id='sources-help', markup=False)

    def on_mount(self):
        self.app.sources_view = self
        self.update_controls()
        self.request('inspect')

    def on_unmount(self):
        self.closed = True
        self.request_generation += 1
        if self.app.sources_view is self:
            self.app.sources_view = None
        pending = self.app.sources_request
        if pending is not None and pending[0] is self:
            self.app.sources_request = None

    def action_detach(self):
        self.app.exit()

    def action_back(self):
        self.dismiss(None)

    def invalidate(self):
        self.expired = True
        self.request_generation += 1
        self.validation = None
        self.query_one('#sources-validation', Static).update('Selected attempt or declarations changed. Reopen Sources for the current job.')
        self.update_controls()

    def request(self, kind, payload=None):
        if self.closed or self.expired or self.busy:
            return
        self.request_generation += 1
        self.busy = True
        self.query_one('#sources-validation', Static).update('Checking bounded source metadata…')
        self.update_controls()
        self.app.request_sources(self, kind, payload)

    async def accept_result(self, kind, result, error, generation):
        if self.closed or self.expired or generation != self.request_generation:
            return
        self.busy = False
        if error:
            self.validation = None
            self.query_one('#sources-validation', Static).update(literal(error))
        elif kind == 'inspect':
            self.inspection = result
            self.plans = result.plans
            self.render_inspection()
            await self.build_fields()
            if result.context.binding_status == 'SOURCE_UNRESOLVED' and self.plans:
                self.configuring = True
                self.render_inspection()
            self.query_one('#sources-validation', Static).update('')
        elif kind == 'validate':
            self.validation = result
            self.validated_fields = self.field_values()
            messages = tuple(result.errors) + tuple(result.warnings)
            lead = ('Validation failed.' if result.errors else
                    'Validated; confirm the reviewed conflict / override below.' if result.conflict else
                    'Validated; not yet active. Review, then Use This Session.')
            self.query_one('#sources-validation', Static).update(literal('\n'.join((lead, *messages))))
        elif kind == 'activate':
            self.closed = True
            self.dismiss(result)
            return
        self.update_controls()
        if kind == 'validate' and result is not None and not result.errors and result.conflict:
            self.query_one('#sources-confirm', Checkbox).scroll_visible(animate=False)

    def render_inspection(self):
        context = self.inspection.context
        state = context.binding_status
        if get_registration(context.engine) is None:
            state = 'ADAPTER_UNAVAILABLE'
        if self.configuring and not self.details:
            rows = [f'Attempt: {context.attempt_id} · {context.engine}',
                    f'Current association: {state}',
                    'New choice: session-local user declaration · lineage unverified.']
            self.query_one('#sources-inspection', Static).update(literal('\n'.join(rows)))
            return
        rows = [f'Attempt: {context.attempt_id}\nEngine: {context.engine}',
                f'Current association: {state}\nOrigin: {context.association}']
        if state == 'SOURCE_UNRESOLVED':
            rows.append('Output location has not been declared. Configure exact sources below.')
        elif state == 'SOURCE_CONFLICT':
            rows.append('Declarations conflict. Review the exact paths before choosing sources.')
        elif state == 'ADAPTER_UNAVAILABLE':
            rows.append('No registered scientific source adapter is available for this engine.')
        elif not self.plans:
            rows.append('This adapter does not register configurable source fields.')
        for source in context.sources:
            stage = f' · stage {source.stage}' if source.stage else ''
            optional = ' (optional)' if source.optional else ''
            rows.append(f'{source.role}{stage}{optional}\n{source.path}')
        declarations = getattr(self.inspection, 'declarations', ())
        if declarations and (self.details or state == 'SOURCE_CONFLICT'):
            rows.append('Recorded declarations:')
            for source in declarations:
                rows.append(f'{source.role}: {source.path}\n  {source.qualification}'
                            + (f' · stage {source.stage}' if source.stage else ''))
        if self.details:
            rows.extend(context.limitations)
        rows.append('Source paths identify observation files; scientific lineage is not independently verified.')
        self.query_one('#sources-inspection', Static).update(literal('\n\n'.join(rows)))

    async def build_fields(self):
        fields = self.query_one('#sources-fields', Vertical)
        await fields.remove_children()
        if not self.plans:
            return
        widgets = []
        if any(plan.location_role == 'output' and plan.filename for plan in self.plans):
            widgets.extend((Static('Output directory (optional convenience)', markup=False),
                            Input(placeholder='/absolute/output/directory', id='sources-directory'),
                            Button('Propose Exact Paths', id='sources-propose')))
        current = self.inspection.context.sources
        for index, plan in enumerate(self.plans):
            matches = [source for source in current if source.role.split(':', 1)[0] == plan.role]
            source = matches[0] if len(matches) == 1 else None
            label = plan.role + (' (optional)' if plan.optional else '')
            widgets.extend((Static(label, markup=False),
                            Input(value=source.path if source else '', placeholder='/absolute/file/path',
                                  id=f'source-path-{index}')))
            if plan.stages:
                widgets.extend((Static('Stage (when declared)', markup=False),
                                Input(value=source.stage or '' if source else '', placeholder='Stage identity',
                                      id=f'source-stage-{index}')))
        await fields.mount(*widgets)

    def field_values(self):
        return tuple((self.query_one(f'#source-path-{index}', Input).value,
                      self.query_one(f'#source-stage-{index}', Input).value if plan.stages else '')
                     for index, plan in enumerate(self.plans))

    def descriptors(self):
        return tuple(SourceDescriptor(plan.role, path, 'user-declared-session-local', plan.optional,
                                      stage=stage or None)
                     for plan, (path, stage) in zip(self.plans, self.field_values()) if path)

    def update_controls(self):
        if not self.is_mounted:
            return
        ready = self.inspection is not None and not self.expired
        self.query_one('#sources-fields').display = self.configuring and ready
        for name in ('details', 'edit', 'clear'):
            button = self.query_one('#sources-' + name, Button)
            button.display = ready and (name == 'details' or not self.configuring)
            button.disabled = self.busy
        self.query_one('#sources-edit', Button).display = ready and not self.configuring and bool(self.plans)
        self.query_one('#sources-clear', Button).display = (ready and not self.configuring
                                                          and self.app.session_source_for(self.selection) is not None)
        for name in ('validate', 'use'):
            button = self.query_one('#sources-' + name, Button)
            button.display = ready and self.configuring
            button.disabled = self.busy or self.expired
        valid = self.validation is not None and not self.validation.errors
        confirm = self.query_one('#sources-confirm', Checkbox)
        confirm.display = self.configuring and valid and self.validation.conflict
        self.query_one('#sources-use', Button).disabled = (self.busy or not valid
            or self.validated_fields != self.field_values()
            or bool(self.validation.conflict and not confirm.value)) if self.configuring and ready else True

    def on_input_changed(self, event):
        if not self.configuring:
            return
        if event.input.id and event.input.id.startswith('source-'):
            self.validation = None
            self.validated_fields = None
            self.query_one('#sources-confirm', Checkbox).value = False
            self.request_generation += 1
            # The underlying filesystem worker still owns the shared busy slot.
            self.busy = False
            self.query_one('#sources-validation', Static).update('Paths changed. Validate before activation.')
            self.update_controls()

    def on_checkbox_changed(self, event):
        self.update_controls()

    def on_button_pressed(self, event):
        event.stop()
        name = event.button.id
        if name == 'sources-back':
            self.action_back()
        elif name == 'sources-details':
            self.details = not self.details
            self.render_inspection()
        elif name == 'sources-edit':
            self.configuring = True
            self.render_inspection()
            self.update_controls()
            self.query_one('#source-path-0', Input).focus()
        elif name == 'sources-clear':
            self.app.clear_session_source(self)
        elif name == 'sources-propose':
            directory = self.query_one('#sources-directory', Input).value
            if not Path(directory).is_absolute():
                self.query_one('#sources-validation', Static).update('Enter an absolute output directory; no path expansion is performed.')
                return
            for index, plan in enumerate(self.plans):
                if (plan.location_role == 'output' and plan.filename
                        and Path(plan.filename).name == plan.filename and plan.filename not in {'.', '..'}):
                    self.query_one(f'#source-path-{index}', Input).value = str(Path(directory) / plan.filename)
        elif name == 'sources-validate':
            self.request('validate', self.descriptors())
        elif name == 'sources-use' and self.validation is not None:
            if self.validated_fields != self.field_values():
                self.query_one('#sources-validation', Static).update('Paths changed. Validate again.')
                return
            self.request('activate', (self.validation, self.query_one('#sources-confirm', Checkbox).value))
