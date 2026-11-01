"""Small identity-bound portal dialogs; remote paths never imply local authority."""
from __future__ import annotations

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static

from cmw.jobs.copy_ui import CopyPreview
from cmw.jobs.snapshot import render_snapshot
from cmw.jobs.tui import literal


def origin_text(snapshot):
    origin = snapshot.to_dict()['node_origin']
    return (f"Node: {origin['alias']} / queue {origin['queue_alias']}\n"
            f"Identity: {origin['node_id']} / {origin['queue_id']} / {origin['attempt_id']}\n"
            f"Connection: {origin['connection']} · component receipt ages: {origin['receipt_ages']}\n"
            f"Endpoint runtime: {origin['runtime_id']} · clock {origin['clock_id']}\n")


class NodeCopyPreview(CopyPreview):
    def __init__(self, snapshot, identity, refresh):
        super().__init__(snapshot, refresh)
        self.identity = identity
        self.text = origin_text(snapshot) + '\n' + self.text

    def render_capture(self):
        self.text = render_snapshot(self.snapshot, mode=self.mode, redact_paths=self.redact_paths)
        if self.mode != 'json':
            self.text = origin_text(self.snapshot) + '\n' + self.text
        self.query_one('#copy-text').load_text(self.text)


class FetchDialog(ModalScreen):
    BINDINGS = [Binding('escape', 'dismiss(None)', 'Back'), Binding('ctrl+c', 'dismiss(None)', 'Back', priority=True)]
    DEFAULT_CSS = '''
    FetchDialog { align: center middle; background: $background 70%; }
    FetchDialog > Vertical { width: 94%; height: auto; max-height: 95%; border: round $accent; padding: 1; background: $surface; }
    FetchDialog Static { height: auto; }
    FetchDialog Input { margin-bottom: 1; }
    FetchDialog Horizontal { height: 3; }
    '''

    def __init__(self, owner, target, label):
        super().__init__()
        self.owner, self.target, self.label = owner, dict(target), label
        self.busy = False

    def compose(self):
        with Vertical():
            yield Static('FETCH SELECTED ARTIFACTS · ' + self.label, markup=False)
            yield Static('Use an explicit local fetch-spec JSON and a new local destination.\nActive files remain incomplete observations; copies are replicas.', markup=False)
            yield Input(placeholder='/absolute/local/fetch.json', id='fetch-spec')
            yield Input(placeholder='/absolute/new/local/destination', id='fetch-destination')
            yield Static('', id='fetch-status', markup=False)
            with Horizontal():
                yield Button('Fetch', id='fetch-confirm', variant='primary')
                yield Button('Back', id='fetch-back')

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'fetch-back':
            self.dismiss(None)
        elif event.button.id == 'fetch-confirm' and not self.busy:
            self.app.start_fetch(self, self.query_one('#fetch-spec', Input).value,
                                 self.query_one('#fetch-destination', Input).value)


class NodeFiles(ModalScreen):
    BINDINGS = [Binding('escape', 'dismiss(None)', 'Back'), Binding('q', 'dismiss(None)', 'Back'),
                Binding('ctrl+c', 'dismiss(None)', 'Back', priority=True)]
    DEFAULT_CSS = '''
    NodeFiles { align: center middle; background: $background 70%; }
    NodeFiles > Vertical { width: 96%; height: 94%; border: round $accent; padding: 0 1; background: $surface; }
    NodeFiles #node-files-table { height: 1fr; min-height: 4; }
    NodeFiles Static { height: auto; }
    NodeFiles #node-files-detail { max-height: 9; }
    NodeFiles Horizontal { height: 3; }
    NodeFiles Button { min-width: 10; margin-right: 1; }
    '''

    def __init__(self, owner, target, label, result, *, local=False):
        super().__init__()
        self.owner, self.target, self.label = owner, dict(target), label
        self.result, self.local = result, local
        self.locations = {item['location_id']: item for item in result['locations']}

    def compose(self):
        with Vertical():
            yield Static('DECLARED FILE LOCATIONS · ' + self.label, markup=False)
            yield DataTable(id='node-files-table', cursor_type='row', zebra_stripes=True)
            yield Static('', id='node-files-detail', markup=False)
            yield Static('Locations are reported by the execution node; scientific validity is not assessed.', id='node-files-status', markup=False)
            with Horizontal():
                yield Button('Open folder' if self.local else 'Open share', id='node-files-open')
                yield Button('Copy location', id='node-files-copy')
                yield Button('Fetch…', id='node-files-fetch', variant='primary')
                yield Button('Back', id='node-files-back')

    def on_mount(self):
        table = self.query_one(DataTable)
        table.add_columns('ROLE', 'ACCESS', 'NODE PATH')
        for key, item in self.locations.items():
            table.add_row(literal(item['role']), literal(item['accessibility']),
                          literal(item.get('node_path') or item.get('declared_path') or 'unavailable'), key=key)
        table.focus()
        self.render_location()

    def selected_location(self):
        table = self.query_one(DataTable)
        return self.locations.get(table.ordered_rows[table.cursor_row].key.value) if table.row_count else None

    def render_location(self):
        item = self.selected_location()
        text = ('No declared locations.' if item is None else
                f"{self.label}\n{item.get('node_path') or item.get('declared_path')}\n"
                f"{item['role']} · {item['kind']} · {item['accessibility']}\n"
                f"Location ID: {item['location_id']} · storage alias: {item.get('storage_alias') or 'see node configuration'}\n"
                f"Storage: {item.get('storage_id') or 'not mapped'} · {item.get('qualification', '')}\n"
                f"{item.get('detail', '')}")
        self.query_one('#node-files-detail', Static).update(literal(text))

    def on_data_table_row_highlighted(self, event):
        event.stop()
        self.render_location()

    def on_button_pressed(self, event):
        event.stop()
        item = self.selected_location()
        if event.button.id == 'node-files-back':
            self.dismiss(None)
        elif event.button.id == 'node-files-copy' and item:
            self.app.request_copy(self.label + '\n' + (item.get('node_path') or item.get('declared_path') or ''), view=self)
        elif event.button.id == 'node-files-fetch':
            self.app.push_screen(FetchDialog(self.owner, self.target, self.label))
        elif event.button.id == 'node-files-open':
            if self.local and item:
                self.app.open_local_location(self, item)
            else:
                self.query_one('#node-files-status', Static).update('Opening requires a separately qualified share mapping. Use Copy Location or Fetch; this remote path is never opened locally.')


class NodeControlConfirm(ModalScreen):
    BINDINGS = [Binding('escape', 'dismiss(None)', 'Back'), Binding('ctrl+c', 'dismiss(None)', 'Back', priority=True)]
    DEFAULT_CSS = '''
    NodeControlConfirm { align: center middle; background: $background 70%; }
    NodeControlConfirm > Vertical { width: 94%; height: auto; max-height: 95%; border: round $accent; padding: 1; background: $surface; }
    NodeControlConfirm Static { height: auto; }
    NodeControlConfirm Horizontal { height: 3; }
    '''

    def __init__(self, owner, target, label, preview):
        super().__init__()
        self.owner, self.target, self.label, self.preview = owner, dict(target), label, preview
        self.busy = False
        self.request_id = None

    def compose(self):
        with Vertical():
            yield Static('CONFIRM ' + self.preview['action'].upper(), markup=False)
            yield Static(self.label + '\nNode ' + self.target['node_id'] + '\nQueue ' + self.target['queue_id'] +
                         '\nAttempt ' + self.target['attempt_id'], markup=False)
            yield Static('\n'.join(self.preview.get('consequences', ())) or
                         str(self.preview.get('summary') or 'Explicit destination-local action; accepted does not mean completed.'), markup=False)
            yield Input(placeholder='Reason (optional unless requested)', id='node-control-reason')
            yield Static('', id='node-control-status', markup=False)
            with Horizontal():
                yield Button('Confirm', id='node-control-confirm', variant='warning')
                yield Button('Check result', id='node-control-check', disabled=True)
                yield Button('Back', id='node-control-back')

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'node-control-back':
            self.dismiss(None)
        elif event.button.id == 'node-control-check' and self.request_id:
            self.app.check_control_result(self.owner, self.request_id, self)
        elif event.button.id == 'node-control-confirm' and not self.busy:
            self.app.start_control(self, self.query_one('#node-control-reason', Input).value)
