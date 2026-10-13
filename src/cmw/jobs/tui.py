"""Observation-only Textual client; all controls revalidate in the queue store."""
from __future__ import annotations

import json
import time
import unicodedata
from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from pathlib import Path

from rich.cells import cell_len, set_cell_size
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Label, Static, TextArea

from .cli import elapsed, safe_text, name_text, tail, guard_text, external_detail, usage_cells, usage_detail, machine_text, sharing_text, job_sharing_detail, observation_sharing
from .store import ACTIVE, PENDING, JobsError, Store
from .status_presentation import admission_presentation, scheduling_detail
from .locations import discover
from .folder_opener import OpenResult, open_location, request_open
from .history_ui import HistoryDetails, ProgressHistory
from .suspension_ui import SuspensionConfirm
from .suspension_presentation import effective_status, intended_action, suspension_summary, suspension_detail, submission_text


def literal(value):
    return Text(safe_text(value))


def table_name(value, width):
    """Keep both ends of a literal name within the renderer's display-cell budget."""
    text = name_text(value)
    if cell_len(text) <= width:
        return Text(text, no_wrap=True)
    suffix_budget = (width - 1) // 2
    suffix = ""
    for char in reversed(text):
        if cell_len(char + suffix) > suffix_budget:
            break
        suffix = char + suffix
    # A cropped suffix must not begin with an orphan combining/variation mark.
    suffix = suffix.lstrip(" ")
    while suffix and unicodedata.category(suffix[0]).startswith("M"):
        suffix = suffix[1:]
    prefix = set_cell_size(text, width - 1 - cell_len(suffix)).rstrip()
    return Text(prefix + "…" + suffix, no_wrap=True)


def managed_columns(viewport_width, *, bounded=False):
    """Keep identification stable across lifecycle changes; scroll below 59 columns."""
    columns = [('id', 'ID P/A' if bounded and viewport_width < 128 else 'JOB ID', 8),
               ('files', 'FILES', 5), ('name', 'NAME', 12), ('status', 'STATUS', 10), ('elapsed', 'ELAPSED', 10)]
    if viewport_width >= 108:
        columns[-1:-1] = [('cpu_now', 'CPU NOW', 7), ('ram_now', 'RAM NOW', 15)]
    if viewport_width >= 128:
        columns.insert(0, ('order', 'ORDER', 5))
        columns.insert(4, ('role', 'ROLE', 9) if bounded else ('engine', 'ENGINE', 7))
        columns.insert(-3, ('cpus', 'CPUS', 4))
    if viewport_width >= 168:
        columns.append(('reason', 'REASON', 24))
    # Each cell has one space either side; reserve the vertical scrollbar too.
    remaining = viewport_width - 2 - sum(width + 2 for key, _, width in columns if key != 'name') - 2
    return [(key, label, max(12, remaining) if key == 'name' else width) for key, label, width in columns]


def timestamp(value):
    return "—" if value is None else datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def detail(job, *, narrow=False):
    if not job:
        return "No selection. Add a prepared job through cmw jobs add."
    resource = job["resources"]
    cpu_now, ram_now = usage_cells(job.get("usage"))
    resources = (f"Requested: {resource['cpus'] or '—'} CPUs / {resource['memory_gib'] or '—'} GiB\n"
                 f"MPI: {resource['mpi_ranks'] or '—'} ranks × {resource['threads_per_rank'] or '—'} threads/rank\n" if narrow else
                 f"CPUs requested: {resource['cpus'] or '—'}    MPI ranks: {resource['mpi_ranks'] or '—'}    "
                 f"Threads/rank: {resource['threads_per_rank'] or '—'}    RAM requested: {resource['memory_gib'] or '—'} GiB\n")
    return (f"SELECTED: {job['display_id']} / {name_text(job['name'])}\n"
            f"Status: {effective_status(job)}    Order: {job['order'] or '—'}    Engine: {job['engine']}\n"
            f"CPU NOW: {cpu_now}   RAM NOW: {ram_now}\n"
            f"{resources}"
            f"Working directory: {job['cwd']}\n{job['reason'] or '—'}")


class Prompt(ModalScreen):
    BINDINGS = [("escape", "dismiss(None)", "Back")]
    DEFAULT_CSS = """
    Prompt { align: center middle; background: $background 70%; }
    Prompt > Vertical { width: 64; height: auto; max-height: 90%; padding: 1 2; border: round $accent; background: $surface; }
    Prompt Input { margin: 1 0; }
    Prompt Button { margin-top: 1; }
    """

    def __init__(self, message, *, order=False):
        super().__init__()
        self.message, self.order = message, order

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(literal(self.message))
            yield Input(placeholder="New pending order" if self.order else "Type CANCEL to confirm", id="answer")
            yield Button("Apply", id="apply", variant="primary")
            yield Button("Back", id="back")

    def on_mount(self):
        self.query_one(Input).focus()

    def on_input_submitted(self, event):
        self.dismiss(event.value)

    def on_button_pressed(self, event):
        self.dismiss(self.query_one(Input).value if event.button.id == "apply" else None)


class Inspect(ModalScreen):
    BINDINGS = [("escape", "dismiss(None)", "Back"), ("q", "dismiss(None)", "Back"), ("ctrl+c", "dismiss(None)", "Back")]
    DEFAULT_CSS = """
    Inspect { align: center middle; background: $background 70%; }
    Inspect > Vertical { width: 94%; height: 90%; border: round $accent; padding: 1 2; background: $surface; }
    Inspect VerticalScroll { height: 1fr; }
    Inspect Static { height: auto; }
    """

    def __init__(self, store, job_id, logs=False, job_provider=None, progress_provider=None, execution_provider=None):
        super().__init__()
        self.store, self.job_id, self.logs = store, job_id, logs
        self.log_stamps = None
        self.job_provider = job_provider
        self.progress_provider = progress_provider
        self.execution_provider = execution_provider

    def compose(self):
        with Vertical():
            yield Label("LOG TAIL · stdout / stderr · bounded to 16 KiB each" if self.logs else "ATTEMPT DETAILS")
            with VerticalScroll():
                yield Static(id="body", markup=False)
            yield Label("Esc / Q: back · PgUp / PgDn: scroll")

    def on_mount(self):
        self.refresh_content()
        self.set_interval(1 if self.logs else 0.5, self.refresh_content)

    def refresh_content(self):
        job = (self.job_provider(self.job_id) if self.job_provider and not self.logs else
               next((j for j in self.store.snapshot()["jobs"] if j["display_id"] == self.job_id), None))
        if not job:
            self.query_one("#body", Static).update("Job is no longer available")
            return
        if self.logs:
            stamps = []
            for path in job["logs"].values():
                try:
                    stat = Path(path).stat()
                    stamps.append((stat.st_ino, stat.st_size, stat.st_mtime_ns))
                except FileNotFoundError:
                    stamps.append(None)
            if stamps == self.log_stamps:
                return
            self.log_stamps = stamps
            text = "\n\n".join(f"{stream.upper()} · {path}\n{tail(path)}" for stream, path in job["logs"].items())
        else:
            text = (detail(job) + f"\n\nNote (human annotation):\n{job.get('note') or '—'}\n\n"
                    + job_sharing_detail(job) + "\n\n" + usage_detail(job.get("usage")) + f"\n\nAttempt: {job['attempt_id']}\n"
                    f"Enqueued: {timestamp(job['enqueued_at'])}\nStarted: {timestamp(job['started_at'])}\n"
                    f"Finished: {timestamp(job['finished_at'])}\nExit code: {job['exit_code']}   Signal: {job['signal']}\n"
                    f"Failure policy: {job['on_failure']}\nCHECK: Not evaluated\n\n"
                    + json.dumps({key: job[key] for key in ("argv", "env", "cwd", "logs", "worker", "group", "layout")}, indent=2, ensure_ascii=False))
        if not self.logs and job.get("suspension"):
            text += "\n\n" + suspension_detail(job)
        if self.execution_provider and not self.logs and job['status'] in ACTIVE:
            observation = self.execution_provider(job)
            text += "\n\nEXECUTION OBSERVATION — read only\n"
            text += (json.dumps(observation, indent=2, ensure_ascii=False) if observation is not None
                     else "Observation pending / unavailable; stored status is not freshly verified.")
            if self.app.execution_error:
                text += "\n" + self.app.execution_error
        if self.progress_provider and not self.logs:
            snapshot = self.progress_provider(job)
            if snapshot is not None:
                from cmw.progress.formatting import progress_text
                text += "\n\nSCIENTIFIC EVIDENCE — parser success does not verify execution\n" + progress_text(snapshot, detailed=True)
                if self.app.progress_error:
                    text += "\nObservation read unavailable; cached evidence: " + self.app.progress_error
        self.query_one("#body", Static).update(literal(text))


class NoteEditor(ModalScreen):
    """An explicit save targets the captured attempt/revision, never a display label."""
    BINDINGS = [Binding("ctrl+s", "save", "Save", priority=True),
                Binding("escape", "dismiss(None)", "Cancel", priority=True),
                Binding("ctrl+c", "detach", "Detach without saving", priority=True)]
    DEFAULT_CSS = """
    NoteEditor { align: center middle; background: $background 70%; }
    NoteEditor > Vertical { width: 94%; height: 85%; border: round $accent; padding: 1 2; background: $surface; }
    NoteEditor #note-title { height: 1; }
    NoteEditor TextArea { height: 1fr; margin: 1 0; }
    NoteEditor #note-error { height: auto; max-height: 3; color: $warning; }
    NoteEditor Horizontal { height: 3; }
    NoteEditor Button { min-width: 10; margin-right: 1; }
    NoteEditor #note-help { height: auto; }
    """

    def __init__(self, store, job):
        super().__init__()
        self.store = store
        self.job_id, self.attempt_id = job['id'], job['attempt_id']
        self.revision = job.get('note_revision', 0)
        self.initial_note = safe_text(job.get('note', ''))
        self.display_id = job['display_id']

    def compose(self):
        with Vertical():
            yield Label(literal(f"NOTE · {self.display_id} · human annotation"), id="note-title")
            yield TextArea(self.initial_note, id="note-text", tab_behavior="indent")
            yield Static("", id="note-error", markup=False)
            with Horizontal():
                yield Button("Save", id="note-save", variant="primary")
                yield Button("Reload", id="note-reload")
                yield Button("Cancel", id="note-cancel")
            yield Label("Ctrl-S Save · Esc Cancel · Ctrl-C Detach without saving\n4096 characters · Reload replaces your buffer", id="note-help")

    def on_mount(self):
        self.query_one(TextArea).focus()

    def action_save(self):
        try:
            self.store.annotate(self.job_id, self.query_one(TextArea).text,
                                expected_revision=self.revision, expected_attempt_id=self.attempt_id)
        except (ValueError, OSError) as exc:
            self.query_one('#note-error', Static).update(literal(f"Not saved: {exc}. Your text is kept; Reload reads the saved note."))
            return
        self.dismiss(True)

    def action_reload(self):
        try:
            job = next((job for job in self.store.snapshot()['jobs'] if job['id'] == self.job_id), None)
            if job is None or job['attempt_id'] != self.attempt_id:
                raise JobsError("The original job/attempt is no longer available")
        except (ValueError, OSError) as exc:
            self.query_one('#note-error', Static).update(literal(f"Not reloaded: {exc}. Your text is kept."))
            return
        self.revision = job.get('note_revision', 0)
        self.query_one(TextArea).load_text(safe_text(job.get('note', '')))
        self.query_one('#note-error', Static).update("")
        self.query_one(TextArea).focus()

    def action_detach(self):
        self.app.exit()

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'note-save':
            self.action_save()
        elif event.button.id == 'note-reload':
            self.action_reload()
        elif event.button.id == 'note-cancel':
            self.dismiss(None)


class ExternalInspect(ModalScreen):
    BINDINGS = [("escape", "dismiss(None)", "Back"), ("q", "dismiss(None)", "Back"), ("ctrl+c", "dismiss(None)", "Back")]
    DEFAULT_CSS = Inspect.DEFAULT_CSS.replace("Inspect", "ExternalInspect")

    def __init__(self, observation, provider=None):
        super().__init__()
        self.observation = observation
        self.provider = provider

    def compose(self):
        with Vertical():
            yield Label("EXTERNAL OBSERVATION — READ ONLY")
            with VerticalScroll():
                yield Static(literal(external_detail(self.observation)), id="external-body", markup=False)
            yield Label("Esc / Q: back · No managed controls or logs")

    def on_mount(self):
        self.set_interval(0.5, self.refresh_content)

    def refresh_content(self):
        observation = self.provider(self.observation['id']) if self.provider else self.observation
        self.query_one("#external-body", Static).update(literal(
            external_detail(observation) if observation else "External process no longer observed; outcome is not known."))


class SharingInspect(ModalScreen):
    BINDINGS = [("escape", "dismiss(None)", "Back"), ("q", "dismiss(None)", "Back"), ("ctrl+c", "dismiss(None)", "Back")]
    DEFAULT_CSS = Inspect.DEFAULT_CSS.replace("Inspect", "SharingInspect")

    def __init__(self, provider):
        super().__init__()
        self.provider = provider

    def compose(self):
        with Vertical():
            yield Label("SCHEDULING DETAILS — READ ONLY")
            with VerticalScroll():
                yield Static(id="sharing-body", markup=False)
            yield Label("Esc / Q: back · CLI config/sharing/reserve change declarations")

    def on_mount(self):
        self.refresh_content()
        self.set_interval(0.5, self.refresh_content)

    def refresh_content(self):
        state = self.provider()
        text = scheduling_detail(state)
        self.query_one("#sharing-body", Static).update(literal(text))


@dataclass(frozen=True)
class FilesRequest:
    """A UI request is scoped to one state, immutable attempt and visible context."""
    state_directory: str
    job_id: int
    attempt_id: str
    context: tuple
    generation: int


class FilesChooser(ModalScreen):
    BINDINGS = [("escape", "dismiss(None)", "Back"), ("q", "dismiss(None)", "Back"),
                ("ctrl+c", "dismiss(None)", "Back")]
    DEFAULT_CSS = """
    FilesChooser { align: center middle; background: $background 70%; }
    FilesChooser > Vertical { width: 94%; height: 90%; border: round $accent; padding: 1 2; background: $surface; }
    FilesChooser VerticalScroll { height: 1fr; }
    FilesChooser Static { height: auto; }
    FilesChooser Button { margin-bottom: 1; }
    FilesChooser #files-message { color: $warning; margin: 1 0; }
    """

    def __init__(self, request, result, message=""):
        super().__init__()
        self.request, self.result, self.message = request, result, message

    def compose(self):
        with Vertical():
            yield Label(literal(f"FILES · {self.result.display_id} · read-only navigation"))
            yield Static(literal(self.message or "Choose a declared location. Opening grants no process ownership."), id="files-message")
            with VerticalScroll():
                for index, location in enumerate(self.result.locations):
                    stage = f" · Stage: {location.stage}" if location.stage else ""
                    yield Static(literal(f"{location.role}{stage} · {location.accessibility}\n"
                                         f"{location.path or location.declared_path or 'Location unknown'}\n"
                                         f"{location.origin} · {location.qualification}"
                                         + (f"\n{location.detail}" if location.detail else "")))
                    yield Button("Open folder", id=f"files-location-{index}", disabled=location.path is None)
            yield Label("Esc / Q: back · Tab: choose a folder button · full paths shown above")

    def on_unmount(self):
        self.app.files_generation += 1

    def on_button_pressed(self, event):
        event.stop()
        index = int(event.button.id.removeprefix('files-location-'))
        self.app.start_file_open(self.request, self.result, self.result.locations[index], chooser=self)


class JobsTable(DataTable):
    BINDINGS = [Binding("enter", "select_cursor", "Details")]

    async def _on_click(self, event):
        if self.id != "table":
            return
        # Textual 6 uses rendered cell metadata, not RowSelected, for mouse hit tests.
        row, column = event.style.meta.get("row"), event.style.meta.get("column")
        if (isinstance(row, int) and isinstance(column, int) and 0 <= row < self.row_count
                and 0 <= column < len(self.ordered_columns)
                and self.ordered_columns[column].key.value == "files"):
            key = self.ordered_rows[row].key.value
            event.stop()
            event.prevent_default()  # Do not also dispatch DataTable.RowSelected / Details.
            self.move_cursor(row=row, column=column, animate=False)
            if self.app.selected_id != key or self.app.external_selected_id:
                self.app.files_generation += 1
                self.app.suspension_selection_generation += 1
            self.app.external_selected_id = None
            self.app.selected_id = key
            self.app.action_files()

    def on_focus(self):
        previous = self.app.external_selected_id
        if self.id == "external" and self.row_count:
            self.app.external_selected_id = self.app.external_rows[self.cursor_row]
        elif self.id == "table":
            self.app.external_selected_id = None
        if previous != self.app.external_selected_id:
            self.app.files_generation += 1
            self.app.suspension_selection_generation += 1
        self.app.query_one("#selected", Static).update(literal(self.app.selected_text()))
        self.app.sync_progress_selection()


class JobsApp(App):
    TITLE = "CMW / JOBS"
    CSS = """
    Screen { background: #111820; color: #d7e0e8; }
    #heading { height: 1; padding: 0 1; color: #b7d8dd; text-style: bold; }
    #sharing { height: auto; max-height: 4; padding: 0 1; color: #b7d8dd; }
    #machine { height: auto; max-height: 2; padding: 0 1; color: #b7d8dd; }
    #controller { height: auto; min-height: 2; padding: 0 1; color: #a9b9c7; }
    #table { height: 1fr; min-height: 5; margin: 1 1 0 1; }
    DataTable > .datatable--header { background: #22303d; color: #d7e0e8; text-style: bold; }
    DataTable > .datatable--cursor { background: #30475a; color: #ffffff; }
    #guard { height: auto; max-height: 5; padding: 0 1; color: #efbd82; }
    #external-heading { height: 1; padding: 0 1; }
    #external { height: 5; margin: 0 1; }
    #selected { height: auto; max-height: 7; margin: 1 1 0 1; padding: 0 1; border-left: solid #5c8996; }
    #execution { height: auto; max-height: 2; margin: 0 1; padding: 0 1; border-left: solid #5c8996; color: #efbd82; }
    #progress { height: auto; max-height: 11; margin: 0 1; padding: 0 1; border-left: solid #5c8996; }
    #events { height: 5; margin: 1 1 0 1; color: #9caeba; }
    #message { height: auto; max-height: 3; padding: 0 1; color: #efbd82; }
    #command-bar { dock: bottom; height: auto; }
    Footer { dock: none; background: #22303d; }
    #execution-controls { dock: none; height: 1; padding: 0 1; background: #22303d; }
    #suspension-action { height: 1; min-height: 1; width: 23; min-width: 23; border: none; padding: 0; }
    #suspension-summary { height: 1; width: 1fr; padding-left: 1; color: #b7d8dd; }
    #shortcuts { dock: none; height: auto; background: #22303d; color: #d7e0e8; padding: 0 1; }
    """
    BINDINGS = [Binding("enter", "details", "Details"), Binding("f", "files", "Files"), Binding("g", "history", "Progress"), Binding("n", "note", "Note"), Binding("l", "logs", "Logs"), Binding("b", "scheduling", "Scheduling"),
                Binding("p", "dispatch", "Dispatch"), Binding("h", "hold", "Hold"),
                Binding("o", "order", "Order"), Binding("x", "cancel", "Cancel"),
                Binding("u", "suspension", "Suspend/Continue", show=False),
                Binding("q", "quit", "Detach", priority=True), Binding("ctrl+c", "quit", "Detach", show=False, priority=True)]

    def __init__(self, store=None, *, clock=None, observer=None, sampler=None,
                 location_descriptors=(), location_resolvers=None, file_opener=None,
                 progress_sources=(), progress_engine=None, progress_service=None, progress_resolver=None,
                 execution_provider=None):
        super().__init__()
        self.store = store or Store()
        self.clock = clock
        self.state = None
        self.selected_id = None
        self.columns_mode = None
        self.row_ids = []
        self.external_selected_id = None
        self.external_rows = []
        self.external_columns_mode = None
        self.activity_projection = {}
        self.observer = observer
        self.sampler = sampler
        self.job_usage = {}
        self.collecting = False
        self.empty_notice_shown = False
        self.location_descriptors = tuple(location_descriptors)
        self.location_resolvers = location_resolvers
        self.file_opener = file_opener or request_open
        self.files_busy = False
        self.files_generation = 0
        self.progress_sources = tuple(progress_sources)
        self.progress_engine = progress_engine
        self.progress_service = progress_service
        self.progress_resolver = progress_resolver
        self.progress_busy = False
        self.progress_closed = False
        self.progress_generation = 0
        self.progress_selection = None
        self.progress_snapshot = None
        self.progress_error = None
        self.history_view = None
        self.suspension_busy = False
        self.suspension_selection_generation = 0
        self.execution_provider = execution_provider
        self.execution_busy = False
        self.execution_closed = False
        self.execution_generation = 0
        self.execution_selection = None
        self.execution_observation = None
        self.execution_error = None

    def compose(self):
        yield Static("CMW / JOBS                                             LOCAL", id="heading")
        yield Static(id="controller", markup=False)
        yield Static(id="machine", markup=False)
        yield Static(id="sharing", markup=False)
        yield JobsTable(id="table", cursor_type="row", zebra_stripes=True)
        yield Static(id="guard", markup=False)
        yield Static("EXTERNAL ACTIVITY — READ ONLY", id="external-heading", markup=False)
        yield JobsTable(id="external", cursor_type="row", zebra_stripes=True)
        yield Static(id="selected", markup=False)
        yield Static(id="execution", markup=False)
        yield Static(id="progress", markup=False)
        yield Static(id="events", markup=False)
        yield Static("Best-effort guard · Q / Ctrl-C detaches; execution state unchanged.", id="message", markup=False)
        with Vertical(id="command-bar"):
            with Horizontal(id="execution-controls"):
                yield Button(literal("[U] Suspend job"), id="suspension-action")
                yield Static("", id="suspension-summary", markup=False)
            yield Static("Enter Details · F Files · G Progress · N Note · L Logs\nP Dispatch · H Hold · O Order · X Cancel · B Scheduling · Q Detach", id="shortcuts", markup=False)
            yield Footer()

    def on_mount(self):
        self.refresh_state()
        self.query_one(DataTable).focus()
        self.set_interval(0.5, self.refresh_state)
        self.collect_activity()
        self.set_interval(2, self.collect_activity)
        self.set_interval(4, self.collect_progress)
        self.set_interval(4, self.collect_execution)

    def on_unmount(self):
        self.files_generation += 1
        self.execution_closed = True
        self.execution_generation += 1
        self.progress_closed = True
        self.progress_generation += 1
        # Cancelling a Textual worker cannot stop a filesystem thread. Keep the
        # busy gate until its real return, and let that worker release readers.
        if not self.progress_busy and self.progress_service is not None:
            self.progress_service.close()

    def execution_identity(self):
        job = self.selected()
        if (self.external_selected_id or job is None or job['status'] not in ACTIVE
                or not hasattr(self.store, 'root')):
            return None
        from .lifecycle import observation_key
        return observation_key(self.store, job)

    def sync_execution_selection(self):
        if self.execution_closed:
            return
        selection = self.execution_identity()
        if selection != self.execution_selection:
            self.execution_generation += 1
            self.execution_selection = selection
            self.execution_observation = None
            self.execution_error = None
            if not self.execution_busy:
                self.collect_execution()
        self.render_execution()

    def collect_execution(self):
        if self.execution_busy or self.execution_closed:
            return
        selection = self.execution_identity()
        if selection != self.execution_selection:
            self.sync_execution_selection()
            return
        if selection is None:
            return
        generation = self.execution_generation
        job = deepcopy(self.selected())
        state = {'schema': self.state.get('schema'),
                 'controller': deepcopy(self.state.get('controller', {}))}
        now = self.clock() if self.clock else None
        self.execution_busy = True
        def collect():
            observation, error = None, None
            try:
                from .lifecycle import observe_execution
                provider = self.execution_provider or observe_execution
                observation = provider(self.store, job, state=state, now=now)
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
            try:
                if not self.execution_closed:
                    self.call_from_thread(self.accept_execution, selection, generation, observation, error)
            except RuntimeError:
                pass  # Detached clients cannot accept a delayed observation.
            finally:
                if self.execution_closed:
                    self.execution_busy = False
        self.run_worker(collect, thread=True, group="execution-observation", exit_on_error=False)

    def accept_execution(self, selection, generation, observation, error):
        self.execution_busy = False
        if self.execution_closed:
            return
        if generation != self.execution_generation or selection != self.execution_identity():
            self.sync_execution_selection()
            self.set_timer(0.05, self.collect_execution)
            return
        self.execution_observation = observation
        self.execution_error = error
        self.render_execution()

    def selected_execution(self, job):
        if (self.execution_observation is None or self.execution_closed
                or self.execution_selection != self.execution_identity()
                or self.selected() is None or job['attempt_id'] != self.selected()['attempt_id']
                or job['id'] != self.selected()['id']):
            return None
        return self.execution_observation.to_dict(now=self.clock() if self.clock else None)

    def render_execution(self):
        if not self.is_mounted or self.execution_closed:
            return
        widget = self.query_one('#execution', Static)
        widget.display = self.execution_selection is not None
        if not widget.display:
            return
        job = self.selected()
        observation = self.selected_execution(job)
        if observation is None:
            summary = 'Observation unavailable' if self.execution_error else 'Observation pending'
            note = 'Stored status is not freshly verified; Enter details'
        else:
            category = observation['observed_execution']
            summary = {
                'VERIFIED_WORKER': 'Verified worker + anchor',
                'VERIFIED_STOPPED': 'Verified stopped (observed scope)',
                'CONTROLLED_PHASE': 'Worker verified; control phase stored',
                'WORKER_MISSING': 'UNVERIFIED — worker missing',
                'OWNERSHIP_UNVERIFIED': 'UNVERIFIED — ownership',
                'PARTIAL_DOMAIN': 'UNVERIFIED — partial domain',
                'TERMINAL_EVIDENCE_PRESENT': 'Terminal evidence; unresolved',
                'OBSERVATION_UNAVAILABLE': 'Observation unavailable',
                'NOT_APPLICABLE': 'Not applicable',
            }.get(category, 'UNVERIFIED')
            age = observation.get('age_seconds')
            freshness = 'Observation age unavailable' if age is None else f'Observed {int(age)}s ago'
            if observation.get('stale'):
                summary = 'STALE — ' + summary
            note = ('Reconciliation required' if observation.get('reconciliation_required')
                    else 'Scope: worker + anchor only' if category == 'VERIFIED_WORKER'
                    else 'Enter details for observation scope')
            suspension = observation.get('evidence', {}).get('suspension', {})
            if suspension.get('phase') and suspension.get('verdict') != 'not_applicable':
                note = 'Stored phase: ' + str(suspension['phase'])
                if observation.get('reconciliation_required'):
                    note += ' · Reconciliation required'
                elif suspension.get('inhibited'):
                    note += ' · inhibited'
            note += ' · ' + freshness
        width = max(12, self.size.width - 5)
        widget.update(literal(set_cell_size(f"Stored: {job['status']} · Execution: {summary}", width).rstrip()
                              + '\n' + set_cell_size(note, width).rstrip()))

    def progress_identity(self):
        job = self.selected()
        if self.external_selected_id or job is None or not hasattr(self.store, 'root'):
            return None
        declarations = json.dumps({key: job.get(key) for key in ('argv', 'cwd', 'layout', 'engine')}, sort_keys=True)
        return (str(self.store.root), job['id'], job['attempt_id'], declarations,
                self.progress_sources, self.progress_engine, self.location_descriptors)

    def sync_progress_selection(self):
        self.sync_execution_selection()
        selection = self.progress_identity()
        if selection != self.progress_selection:
            if self.history_view is not None and self.history_view.selection != selection:
                self.history_view.invalidate()
            self.progress_generation += 1
            self.progress_selection = selection
            self.progress_snapshot = None
            self.progress_error = None
            if not self.progress_busy:
                self.collect_progress()
        self.render_progress()

    def collect_progress(self):
        if self.progress_busy or self.progress_closed:
            return
        selection = self.progress_identity()
        if selection != self.progress_selection:
            self.sync_progress_selection()
            return
        if selection is None:
            if self.progress_service is not None:
                self.progress_service.retain(())
            return
        generation = self.progress_generation
        history_view = self.history_view
        history_request = ((history_view, history_view.request_generation, history_view.parameters())
                           if history_view is not None and not history_view.closed and not history_view.expired
                           and history_view.selection == selection else None)
        self.progress_busy = True
        def collect():
            snapshot, error, history_page = None, None, None
            try:
                from cmw.progress.service import ObservationService
                from .progress import resolve_context
                if self.progress_service is None:
                    self.progress_service = ObservationService()
                resolver = self.progress_resolver or resolve_context
                context = resolver(self.store, selection[1], expected_attempt_id=selection[2],
                                   descriptors=self.location_descriptors, resolvers=self.location_resolvers,
                                   sources=self.progress_sources, engine=self.progress_engine)
                self.progress_service.retain((context,))
                if history_request:
                    self.call_from_thread(history_request[0].bind_context, context)
                    if not history_request[0].closed and not history_request[0].expired:
                        history_page = self.progress_service.history_page(context, **history_request[2])
                    snapshot = self.progress_service.snapshot(context)
                else:
                    snapshot = self.progress_service.poll(context)
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
            try:
                if not self.progress_closed:
                    self.call_from_thread(self.accept_progress, selection, generation, snapshot, error, history_request, history_page)
            except RuntimeError:
                pass  # Detached clients no longer own a visible request.
            finally:
                if self.progress_closed and self.progress_service is not None:
                    self.progress_service.close()
        self.run_worker(collect, thread=True, group="live-progress", exit_on_error=False)

    def accept_progress(self, selection, generation, snapshot, error, history_request=None, history_page=None):
        self.progress_busy = False
        if self.progress_closed or generation != self.progress_generation or selection != self.progress_identity():
            # No replacement starts while an earlier underlying read is blocked.
            if self.progress_service is not None:
                self.progress_service.retain(())
            self.sync_progress_selection()
            return
        if snapshot is not None:
            self.progress_snapshot = snapshot
        self.progress_error = error
        self.render_progress()
        current_history = self.history_view
        if history_request and history_page is not None:
            view, request_generation, _ = history_request
            if current_history is view and not view.closed:
                view.accept_page(history_page.context, history_page, request_generation)
            elif self.progress_service is not None:
                self.progress_service.release_history(history_page.context)
        history_pending = (current_history is not None and not current_history.closed and not current_history.expired
                           and (history_request is None or current_history is not history_request[0]
                                or current_history.request_generation != history_request[1]
                                or history_page is not None and history_page.status == 'LOADING'))
        if history_pending or snapshot is not None and (snapshot.completeness == 'CATCHING_UP' or any(source.has_more for source in snapshot.sources)):
            # Catch up once in bounded background slices, then return to 4s polls.
            self.set_timer(0.05, self.collect_progress)

    def selected_progress(self, job):
        snapshot = self.progress_snapshot
        if (snapshot is None or snapshot.context.attempt_id != job['attempt_id']
                or snapshot.context.job_id != job['id']):
            return None
        return snapshot

    def render_progress(self):
        if not self.is_mounted:
            return
        widget = self.query_one('#progress', Static)
        widget.display = self.progress_selection is not None
        compact = self.size.width < 110 or self.size.height < 36
        line_budget = 3 if self.size.width < 60 else (5 if self.execution_selection is not None else 6) if compact else 11
        if compact and self.query_one('#external', DataTable).display:
            line_budget = 3
        if self.progress_error:
            line_budget = max(4, line_budget)
        widget.styles.max_height = line_budget
        if self.progress_snapshot is not None:
            from cmw.progress.formatting import progress_text
            width = max(20, self.size.width - 5)
            display_snapshot = replace(self.progress_snapshot, read_health="Parser: " + self.progress_snapshot.read_health)
            # Reserve the longer scientific label before the formatter places
            # bounded context and essential trailing qualifiers such as pending.
            text = progress_text(display_snapshot, compact=compact, width=width - 4,
                                 now=self.clock() if self.clock else None, max_lines=line_budget - bool(self.progress_error))
            lines = text.splitlines()
            if lines and lines[0].startswith("ACTIVE: "):
                lines[0] = set_cell_size("Scientific: " + lines[0][8:], width).rstrip()
                text = "\n".join(lines)
            if self.progress_error:
                error = table_name('Read unavailable; cached evidence: ' + self.progress_error, width).plain
                text = error + '\n' + text
        else:
            text = 'Progress: ' + (self.progress_error or 'awaiting bounded observation…')
        job = self.selected()
        if job and effective_status(job) == 'Suspended':
            text = 'Suspended: output inactivity is expected; source age remains actual.\n' + text
            widget.styles.max_height = line_budget + 1
        widget.update(literal(text))

    def collect_activity(self):
        if self.collecting:
            return
        self.collecting = True
        def collect():
            try:
                from .activity import DEFAULT_OBSERVER, project
                if self.observer is None:
                    self.observer = DEFAULT_OBSERVER
                if self.sampler is None:
                    from .telemetry import Sampler
                    self.sampler = Sampler()
                state = project(self.store, observer=self.observer, sampler=self.sampler)
                projection = {key: state[key] for key in ("external_activity", "admission", "controller_guard", "machine_usage", "sharing_evidence", "scheduler", "mode") if key in state}
                projection["job_usage"] = {j["attempt_id"]: j.get("usage") for j in state["jobs"] if j["status"] in ACTIVE}
            except Exception as exc:
                # A client collection failure must not erase the last observed processes.
                guard = deepcopy(self.activity_projection.get("external_activity", {}))
                guard.update(state="UNAVAILABLE", reason=f"Client observation failed: {exc}", stale=True,
                             source="client", scope="current local user / recognized accessible executables")
                guard.setdefault("observations", [])
                projection = {**deepcopy(self.activity_projection), "external_activity": guard, "admission": {"permitted": False, "reason": guard["reason"]}}
            self.call_from_thread(self.accept_activity, projection)
        self.run_worker(collect, thread=True, group="external-observation", exit_on_error=False)

    def accept_activity(self, projection):
        self.activity_projection = deepcopy(projection)
        self.job_usage = self.activity_projection.get("job_usage", {})
        self.collecting = False
        self.refresh_state()

    def external_observation(self, observation_id):
        item = next((item for item in self.activity_projection.get("external_activity", {}).get("observations", [])
                     if item["id"] == observation_id), None)
        return {**item, "scheduling_context": observation_sharing({**(self.state or {}), "admission": self.activity_projection.get('admission', {})})} if item else None

    def external_selected(self):
        return self.external_observation(self.external_selected_id)

    def current_job(self, job_id):
        return next((job for job in (self.state or {}).get("jobs", []) if job["display_id"] == job_id), None)

    def refresh_usage(self):
        from .telemetry import age_usage
        self.activity_projection["machine_usage"] = age_usage(self.activity_projection.get("machine_usage"))
        active_ids = {job['attempt_id'] for job in self.state['jobs'] if job['status'] in ACTIVE}
        self.job_usage = {key: value for key, value in self.job_usage.items() if key in active_ids}
        for job in self.state['jobs']:
            job['usage'] = age_usage(self.job_usage.get(job['attempt_id'])) if job['status'] in ACTIVE else None
        for item in self.activity_projection.get('external_activity', {}).get('observations', []):
            item['usage'] = age_usage(item.get('usage'))
        self.query_one("#machine", Static).update(literal(machine_text(self.activity_projection.get("machine_usage"))))

    def selected_text(self):
        item = self.external_selected()
        if item:
            cpu_now, ram_now = usage_cells(item.get('usage'))
            return (f"EXTERNAL: {item['id']} / {item['engine']} — READ ONLY\n"
                    f"Ownership: {item.get('ownership', 'External / unattributed')} · PID {item['pid']}\n"
                    f"CPU NOW: {cpu_now}   RAM NOW: {ram_now}\n"
                    f"NPROC: {item.get('nproc', 1)} observed processes · Process age: {elapsed(item.get('age_seconds'))}\n"
                    f"Working directory: {item.get('cwd') or '—'}\nEnter: measurement quality and evidence")
        job = self.selected()
        if not job:
            return detail(None)
        width = max(12, self.size.width - 6)
        role = (f" [{job.get('scheduling', {}).get('role', 'primary').title()}]"
                if self.state.get('mode') == 'Bounded Sharing' else '')
        phase = effective_status(job)
        phase_label = f" [{phase}]" if phase != job['status'] else ""
        prefix = f"SELECTED: {job['display_id']}{role}{phase_label} / "
        heading = prefix + table_name(job['name'], max(1, width - cell_len(prefix))).plain
        lines = detail(job, narrow=self.size.width < 110).splitlines()
        lines[0] = heading
        if job['status'] in ACTIVE and (self.size.width < 110 or self.size.height < 36):
            return '\n'.join((heading, lines[2]))
        if job.get('note'):
            preview = 'Note: ' + table_name(job['note'], max(1, width - 6)).plain
            if self.size.width < 110 or self.size.height < 36:
                return '\n'.join((heading, preview, lines[2]))
            lines.insert(1, preview)
        return '\n'.join(lines)

    def external_read_only(self):
        if self.external_selected_id:
            self.query_one("#message", Static).update("External observation is read-only: no notes, logs, cancel, hold, or order actions.")
            return True
        return False

    def display_state(self):
        return {**self.activity_projection, **(self.state or {}), "admission": self.activity_projection.get("admission", {})}

    def refresh_external(self, wide):
        guard = self.activity_projection.get("external_activity")
        if guard and guard.get("observed_at") is not None:
            guard["age_seconds"] = max(0, time.time() - guard["observed_at"])
            from .activity import MAX_AGE
            if guard["age_seconds"] > MAX_AGE:
                guard.update(state="UNAVAILABLE", stale=True, reason="Client observation is stale")
                self.activity_projection["admission"] = {"permitted": False, "reason": guard["reason"]}
        if guard:
            from .activity import admission
            self.activity_projection["admission"] = admission(self.state, guard)
            decision = self.activity_projection["admission"]
            for job in self.state['jobs']:
                if job['status'] in PENDING and job['id'] == decision.get('candidate_id'):
                    job['reason'] = decision['reason']
        display_state = self.display_state()
        presentation = admission_presentation(display_state)
        tones = {'neutral': '#a9b9c7', 'caution': '#efbd82', 'warning': '#ea9595'}
        summary = self.query_one("#guard", Static)
        summary.update(table_name(presentation.summary, max(12, self.size.width - 2)))
        summary.styles.color = tones[presentation.tone]
        compact = self.size.width < 110 or self.size.height < 36
        sharing = self.query_one("#sharing", Static)
        sharing.display = self.state.get('mode') == 'Bounded Sharing'
        sharing.styles.max_height = 1 if compact else 4
        sharing.update(literal(sharing_text(display_state, compact=compact)))
        table = self.query_one("#external", DataTable)
        observations = (guard or {}).get("observations", [])
        external_heading = self.query_one("#external-heading", Static)
        external_heading.update(table_name(presentation.external_summary, max(12, self.size.width - 2)))
        external_heading.styles.color = tones[presentation.external_tone]
        table.display = bool(observations)
        columns = ([('id', 'OBS ID', 14), ('engine', 'ENGINE', 6), ('pid', 'PID', 7), ('nproc', 'NPROC', 5),
                    ('status', 'OS STATE', 9), ('cpu_now', 'CPU NOW', 7), ('ram_now', 'RAM NOW', 15), ('age', 'PROCESS AGE', 11), ('exe', 'EXECUTABLE', 32)] if wide else
                   [('id', 'OBS ID', 12), ('engine', 'ENGINE', 5), ('cpu_now', 'CPU NOW', 7), ('ram_now', 'RAM NOW', 15), ('age', 'AGE', 8)])
        if wide != self.external_columns_mode:
            table.clear(columns=True)
            for key, label, width in columns:
                table.add_column(label, key=key, width=width)
            self.external_columns_mode = wide
            self.external_rows = []
        ids = [item['id'] for item in observations]
        for key in list(self.external_rows):
            if key not in ids:
                table.remove_row(key)
                self.external_rows.remove(key)
        for item in observations:
            cpu_now, ram_now = usage_cells(item.get('usage'))
            values = dict(item, age=elapsed(item.get('age_seconds')), cpu_now=cpu_now, ram_now=ram_now)
            cells = [literal(values.get(column) or '—') for column, _, _ in columns]
            if item['id'] not in self.external_rows:
                table.add_row(*cells, key=item['id'])
                self.external_rows.append(item['id'])
            else:
                for (column, _, _), cell in zip(columns, cells):
                    if table.get_cell(item['id'], column) != cell:
                        table.update_cell(item['id'], column, cell)
        if self.external_selected_id and self.external_selected_id not in ids:
            self.external_selected_id = None
            self.query_one("#message", Static).update("Selected external process is no longer observed; outcome is not known.")
        if self.external_selected_id in self.external_rows:
            table.move_cursor(row=self.external_rows.index(self.external_selected_id), animate=False, scroll=False)

    def on_resize(self):
        self.files_generation += 1
        if self.is_mounted:
            self.call_after_refresh(self.refresh_state)

    def selected(self):
        return next((j for j in (self.state or {}).get("jobs", []) if j["display_id"] == self.selected_id), None)

    def refresh_state(self):
        previous_job = self.selected()
        previous_files_context = self.files_context(previous_job) if previous_job else None
        table = self.query_one(DataTable)
        # Cursor movement precedes its queued highlight event; preserve that newer identity.
        if table.has_focus and 0 <= table.cursor_row < len(self.row_ids):
            self.selected_id = self.row_ids[table.cursor_row]
        try:
            self.state = self.store.snapshot(now=self.clock() if self.clock else None)
        except (ValueError, OSError) as exc:
            self.query_one("#message", Static).update(literal(f"State unavailable: {exc}"))
            return
        self.state['sharing_evidence'] = self.activity_projection.get('sharing_evidence')
        self.refresh_usage()
        control = self.state["controller"]
        status = "STALE — needs attention" if control["stale"] else "Online" if control["online"] else "Offline"
        jobs = self.state["jobs"]
        active = [j for j in jobs if j["status"] in ACTIVE]
        cpu = sum(j["resources"]["cpus"] or 0 for j in active) or "—"
        ram = sum(j["resources"]["memory_gib"] or 0 for j in active) or "—"
        age = "never" if control["age_seconds"] is None else f"{int(control['age_seconds'])}s ago"
        compact = self.size.width < 110 or self.size.height < 36
        mode = self.state.get('mode', 'Sequential')
        bounded = mode == 'Bounded Sharing'
        counts = (f"Run: {sum(j['status']=='Run' for j in jobs)}   Queue: {sum(j['status']=='Queue' for j in jobs)}   "
                  f"Hold: {sum(j['status']=='Hold' for j in jobs)}")
        header = (f"Controller: {status}   Dispatch intent: {'ON' if control['dispatch'] else 'OFF'}\nManaged: {counts} · {mode}" if compact else
                  f"Controller: {status}   Dispatch intent: {'ON' if control['dispatch'] else 'OFF'}   Mode: {mode}   Updated: {age}\n"
                  f"Managed requested: CPUs {cpu}   RAM: {ram} GiB   {counts}")
        self.query_one("#controller", Static).update(literal(header))
        self.query_one("#events", Static).display = not compact
        tiny = self.size.width < 60
        self.query_one("#controller", Static).styles.max_height = 2 if tiny else None
        self.query_one("#machine", Static).styles.max_height = 1 if tiny else 2
        self.query_one("#message", Static).styles.max_height = 1 if tiny else 3
        self.query_one("#guard", Static).styles.max_height = 1 if tiny else 3 if compact else 5
        self.query_one("#external", DataTable).styles.height = 4 if compact else 5
        self.query_one("#selected", Static).styles.max_height = 2 if tiny else 3
        self.query_one("#selected", Static).styles.margin = (0 if compact else 1, 1, 0, 1)
        table.styles.min_height = 3 if compact else 5
        table.display = bool(jobs)
        wide = self.size.width >= 110
        self.refresh_external(wide)
        self.query_one(Footer).display = wide
        self.query_one("#shortcuts", Static).display = not wide
        viewport_width = max(1, self.size.width - 2)
        columns = managed_columns(viewport_width, bounded=bounded)
        # Rebuild only on layout/mode changes; refreshes update cells in place.
        layout_mode = tuple(columns)
        if layout_mode != self.columns_mode:
            table.clear(columns=True)
            for key, label, width in columns:
                table.add_column(label, key=key, width=width)
            self.columns_mode = layout_mode
            self.row_ids = []
        for job in jobs:
            key = job['display_id']
            cpu_now, ram_now = usage_cells(job.get('usage'))
            role = job.get('scheduling', {}).get('role', 'primary')
            values = {'files': '[F]', 'order': str(job['order'] or '—').rjust(5), 'id': key + (' A' if role == 'auxiliary' else ' P') if bounded and viewport_width < 128 else key, 'name': job['name'], 'engine': job['engine'],
                      'role': role.title(),
                      'status': effective_status(job), 'cpus': str(job['resources']['cpus'] or '—').rjust(4),
                      'cpu_now': cpu_now, 'ram_now': ram_now,
                      'elapsed': elapsed(job['elapsed']).rjust(10), 'reason': job['reason'] or '—'}
            cells = []
            for column, _, width in columns:
                cell = table_name(values[column], width) if column == 'name' else literal(values[column])
                if column == 'status':
                    cell.stylize({'Run':'#86c6b0', 'Queue':'#b9c8dc', 'Hold':'#d9bc7e', 'Fail':'#ea9595',
                                  'Unknown':'bold #f3b875', 'Cancelling':'#e9ba94', 'Done':'#8baaa0',
                                  'Suspended':'#b7d8dd', 'Suspending':'#e9ba94', 'Resuming':'#e9ba94', 'Unverified':'bold #f3b875'}.get(effective_status(job), '#a1afbc'))
                cells.append(cell)
            if key not in self.row_ids:
                table.add_row(*cells, key=key)
                self.row_ids.append(key)
            else:
                for (column, _, _), cell in zip(columns, cells):
                    if table.get_cell(key, column) != cell:
                        table.update_cell(key, column, cell)
        if not self.selected_id and jobs:
            self.selected_id = jobs[0]['display_id']
        if self.selected_id in self.row_ids and table.cursor_row != self.row_ids.index(self.selected_id):
            table.move_cursor(row=self.row_ids.index(self.selected_id), animate=False, scroll=False)
        self.query_one("#selected", Static).update(literal(self.selected_text()))
        events = self.state['events'][-3:]
        self.query_one("#events", Static).update(literal("EVENTS\n" + "\n".join(
            f"{datetime.fromtimestamp(e['time']).strftime('%H:%M:%S')}  {'J'+str(e['job_id'])+'.1' if e['job_id'] else 'Controller'}  {e['message']}" for e in events)))
        if not jobs and not self.empty_notice_shown:
            self.query_one("#message", Static).update("No managed jobs. Add, then start explicitly. B: guard details; Q: detach.")
            self.empty_notice_shown = True
        elif jobs:
            self.empty_notice_shown = False

        self.refresh_suspension_controls()
        current_job = self.selected()
        current_files_context = self.files_context(current_job) if current_job else None
        if previous_files_context != current_files_context:
            self.files_generation += 1
        self.sync_progress_selection()

    def on_data_table_row_selected(self, event):
        if event.data_table.id not in ("table", "external"):
            return
        if event.data_table.id == "external":
            self.external_selected_id = event.row_key.value
        else:
            self.external_selected_id = None
            self.selected_id = event.row_key.value
        self.action_details()

    def on_data_table_row_highlighted(self, event):
        if event.data_table.id not in ("table", "external"):
            return
        rows = self.external_rows if event.data_table.id == "external" else self.row_ids
        # Layout rebuilds queue transient highlights before restoring the cursor.
        # Only its current row may change the selected observation or open history.
        if (event.cursor_row != event.data_table.cursor_row
                or not 0 <= event.cursor_row < len(rows)
                or rows[event.cursor_row] != event.row_key.value):
            return
        previous = (self.selected_id, self.external_selected_id)
        if event.data_table.id == "external":
            if not event.data_table.has_focus:
                return
            self.external_selected_id = event.row_key.value
        else:
            if self.query_one("#external", DataTable).has_focus:
                return
            self.external_selected_id = None
            self.selected_id = event.row_key.value
        if previous != (self.selected_id, self.external_selected_id):
            self.files_generation += 1
            self.suspension_selection_generation += 1
        self.query_one("#selected", Static).update(literal(self.selected_text()))
        self.refresh_suspension_controls()
        self.sync_progress_selection()

    def apply(self, function):
        try:
            function()
            self.query_one("#message", Static).update("Operation accepted.")
        except (ValueError, OSError) as exc:
            self.query_one("#message", Static).update(literal(f"Not applied: {exc}"))
        self.refresh_state()

    def action_scheduling(self):
        self.push_screen(SharingInspect(self.display_state))

    def check_action(self, action, parameters):
        # App priority bindings run before focused text widgets, including Q.
        if isinstance(self.screen, (NoteEditor, FilesChooser, ProgressHistory, HistoryDetails, SuspensionConfirm)) and action in {
            'details', 'files', 'history', 'note', 'logs', 'scheduling', 'dispatch', 'hold', 'order', 'cancel', 'quit', 'suspension'
        }:
            return False
        return super().check_action(action, parameters)

    def files_context(self, job):
        # No path probing here: discovery and the final accessibility check run in workers.
        declarations = json.dumps({key: job.get(key) for key in ('argv', 'env', 'cwd', 'logs', 'layout')},
                                  sort_keys=True)
        rows = tuple((j['id'], j['attempt_id'], j.get('order')) for j in (self.state or {}).get('jobs', []))
        return (self.selected_id, self.external_selected_id, tuple(self.size), rows, declarations)

    def files_current(self, request):
        job = self.selected()
        screen_valid = (not isinstance(self.screen, ModalScreen)
                        or isinstance(self.screen, FilesChooser) and self.screen.request == request)
        return (screen_valid and job is not None and request.generation == self.files_generation
                and str(self.store.root) == request.state_directory
                and job['id'] == request.job_id and job['attempt_id'] == request.attempt_id
                and self.files_context(job) == request.context)

    def files_message(self, message, chooser=None):
        target = chooser.query_one('#files-message', Static) if chooser and chooser.is_mounted else self.query_one('#message', Static)
        target.update(literal(message))

    def action_files(self):
        if self.external_selected_id:
            self.files_message("Files navigation is currently available for managed jobs only; external activity remains read-only.")
            return
        job = self.selected()
        if job is None:
            return
        if self.files_busy:
            self.files_message("A Files request is still being checked; try again when it finishes.")
            return
        request = FilesRequest(str(self.store.root), job['id'], job['attempt_id'],
                               self.files_context(job), self.files_generation)
        self.files_busy = True
        self.files_message("Checking declared file locations…")
        def collect():
            try:
                result = discover(self.store, request.job_id, expected_attempt_id=request.attempt_id,
                                  descriptors=self.location_descriptors, resolvers=self.location_resolvers)
            except (ValueError, OSError) as exc:
                self.call_from_thread(self.accept_files, request, None, str(exc))
            else:
                self.call_from_thread(self.accept_files, request, result, None)
        self.run_worker(collect, thread=True, group="files-navigation", exit_on_error=False)

    def accept_files(self, request, result, error):
        self.files_busy = False
        if not self.files_current(request):
            self.files_message("Files request expired after the selection, layout or job changed. Select Files again.")
            return
        if error:
            self.files_message(f"Cannot inspect Files: {error}")
            return
        preferred = next((location for location in result.locations if location.key == result.preferred_key), None)
        if preferred and preferred.accessibility == 'AVAILABLE':
            self.start_file_open(request, result, preferred)
        else:
            message = (f"{preferred.accessibility}: {preferred.path or preferred.declared_path}. Choose an alternative below."
                       if preferred else "No unique current output. Choose a role and stage below.")
            self.push_screen(FilesChooser(request, result, message))

    def start_file_open(self, request, result, location, chooser=None):
        if not self.files_current(request):
            self.files_message("Files selection expired. Close this chooser and select Files again.", chooser)
            return
        if self.files_busy:
            self.files_message("A Files request is still being checked; try again when it finishes.", chooser)
            return
        self.files_busy = True
        self.files_message(f"Checking {location.path or location.declared_path}…", chooser)
        def guarded_opener(path):
            try:
                current = self.call_from_thread(self.files_current, request)
            except RuntimeError:
                current = False  # The application was detached while the worker checked paths.
            if not current:
                return OpenResult('LOCATION_CHANGED', 'Files selection changed before opening', path)
            return self.file_opener(path)
        def opening():
            try:
                opened = open_location(self.store, location, descriptors=self.location_descriptors,
                                       resolvers=self.location_resolvers, opener=guarded_opener)
            except (ValueError, OSError) as exc:
                opened = OpenResult('OPEN_FAILED', str(exc), location.path)
            self.call_from_thread(self.accept_file_open, request, result, opened, chooser)
        self.run_worker(opening, thread=True, group="files-navigation", exit_on_error=False)

    def accept_file_open(self, request, result, opened, chooser):
        self.files_busy = False
        if not self.files_current(request):
            self.files_message("Files response expired after the selection, layout or job changed.", chooser)
            return
        message = f"{opened.status}: {opened.reason}" + (f"\n{opened.path}" if opened.path else "")
        if opened.status != 'OPEN_REQUESTED' and chooser is None:
            self.push_screen(FilesChooser(request, result, message))
        else:
            self.files_message(message, chooser)

    @staticmethod
    def suspension_target(job):
        record = job.get('suspension') or {}
        return (job['id'], job['attempt_id'], job['status'], record.get('phase', 'Run'),
                record.get('revision', 0), deepcopy(job.get('worker')))

    def refresh_suspension_controls(self):
        button = self.query_one('#suspension-action', Button)
        job = self.selected() if not self.external_selected_id else None
        phase = effective_status(job) if job else None
        action = intended_action(job) if job else 'suspend'
        button.label = literal('[U] ' + action.title() + ' job')
        button.disabled = (job is None or job.get('status') != 'Run'
                           or phase in {'Suspending', 'Resuming', 'Unverified'} or self.suspension_busy)
        summary = suspension_summary(job, compact=self.size.width < 110) if job else ''
        self.query_one('#suspension-summary', Static).update(table_name(summary, max(10, self.size.width - 27)))

    def suspension_current(self, request):
        job = self.selected()
        screen_valid = (not isinstance(self.screen, ModalScreen)
                        or isinstance(self.screen, SuspensionConfirm) and self.screen.request == request)
        return (screen_valid and not self.external_selected_id and job is not None
                and self.suspension_target(job) == request['target']
                and str(self.store.root) == request['state_directory']
                and request['generation'] == self.suspension_selection_generation)

    def action_suspension(self):
        if isinstance(self.screen, ModalScreen) or self.suspension_busy:
            return
        if self.external_read_only():
            return
        job = self.selected()
        if job is None:
            return
        if effective_status(job) in {'Suspending', 'Resuming', 'Unverified'}:
            self.query_one('#message', Static).update('Execution transition is pending or unverified; inspect Details. Confirmed cancellation remains available with X.')
            return
        request = dict(target=self.suspension_target(job), state_directory=str(self.store.root),
                       job_id=job['id'], attempt_id=job['attempt_id'], action=intended_action(job),
                       generation=self.suspension_selection_generation)
        self.suspension_busy = True
        self.refresh_suspension_controls()
        def inspect():
            from .suspension import readiness
            try:
                result = readiness(self.store, request['job_id'], request['action'])
            except (ValueError, OSError) as exc:
                self.call_from_thread(self.accept_suspension_readiness, request, None, str(exc))
            else:
                self.call_from_thread(self.accept_suspension_readiness, request, result, None)
        self.run_worker(inspect, thread=True, group='execution-control', exit_on_error=False)

    def accept_suspension_readiness(self, request, result, error):
        self.suspension_busy = False
        self.refresh_suspension_controls()
        if not self.suspension_current(request):
            self.query_one('#message', Static).update('Execution selection changed; open U again for a fresh confirmation.')
            return
        if error:
            self.query_one('#message', Static).update(literal('Readiness unavailable: ' + error))
            return
        if (result.get('attempt_id') != request['attempt_id']
                or result.get('revision') != request['target'][4]
                or result.get('phase') != request['target'][3]):
            self.query_one('#message', Static).update('Execution state changed while checking readiness; open U again.')
            return
        request = {**request, 'revision': result['revision'], 'phase': result['phase'],
                   'authority': deepcopy(result.get('authority'))}
        self.push_screen(SuspensionConfirm(request, result))

    def submit_suspension(self, screen, reason):
        request = screen.request
        if screen.busy:
            return
        if not self.suspension_current(request):
            screen.message('This confirmation is stale after the selection or execution state changed. Close it and open U again.')
            screen.query_one('#suspension-confirm', Button).disabled = True
            return
        screen.busy = True
        screen.query_one('#suspension-confirm', Button).disabled = True
        screen.message('Submitting the captured request; completion requires worker verification…')
        def submitting():
            from .suspension import submit
            try:
                result = submit(self.store, request['job_id'], request['action'], reason=reason,
                                expected_attempt_id=request['attempt_id'], expected_revision=request['revision'],
                                expected_phase=request['phase'], expected_authority=request['authority'])
            except (ValueError, OSError) as exc:
                self.call_from_thread(self.accept_suspension_submission, screen, None, str(exc))
            else:
                self.call_from_thread(self.accept_suspension_submission, screen, result, None)
        self.run_worker(submitting, thread=True, group='execution-control', exit_on_error=False)

    def accept_suspension_submission(self, screen, result, error):
        if screen is not self.screen or not screen.is_mounted:
            return
        if error:
            screen.message('Not applied: ' + error + '. Close and reopen U to recheck readiness.')
            return
        screen.dismiss(None)
        self.refresh_state()
        self.query_one('#message', Static).update(literal(submission_text(result)))

    def on_button_pressed(self, event):
        if event.button.id == 'suspension-action':
            self.action_suspension()

    def action_note(self):
        if self.external_read_only():
            return
        selected = self.selected()
        if not selected:
            return
        try:
            job = next((job for job in self.store.snapshot()['jobs'] if job['id'] == selected['id']), None)
            if job is None or job['attempt_id'] != selected['attempt_id']:
                raise JobsError("The selected job/attempt is no longer available")
        except (ValueError, OSError) as exc:
            self.query_one('#message', Static).update(literal(f"Cannot edit note: {exc}"))
            return
        self.push_screen(NoteEditor(self.store, job), lambda saved: self.refresh_state())

    def action_history(self):
        if self.external_read_only():
            return
        selection = self.progress_identity()
        if selection is not None and self.selected_id:
            self.push_screen(ProgressHistory(selection, self.selected_id))

    def action_details(self):
        if self.external_selected():
            self.push_screen(ExternalInspect(self.external_selected(), provider=self.external_observation))
            return
        if self.selected_id:
            self.push_screen(Inspect(self.store, self.selected_id, job_provider=self.current_job, progress_provider=self.selected_progress, execution_provider=self.selected_execution))

    def action_logs(self):
        if self.external_read_only():
            return
        if self.selected_id:
            self.push_screen(Inspect(self.store, self.selected_id, logs=True))

    def action_dispatch(self):
        self.apply(lambda: self.store.dispatch(not self.state['controller']['dispatch']))

    def action_hold(self):
        if self.external_read_only():
            return
        job = self.selected()
        if job:
            self.apply(lambda: self.store.change(job['display_id'], 'release' if job['status']=='Hold' else 'hold', expected=job['status']))

    def action_order(self):
        if self.external_read_only():
            return
        job = self.selected()
        if not job:
            return
        if job['status'] not in PENDING:
            self.query_one("#message", Static).update("Only pending jobs can be reordered.")
            return
        def submit(value):
            if value is not None:
                self.apply(lambda: self.store.change(job['display_id'], 'move', position=int(value), expected=job['status']))
        self.push_screen(Prompt(f"Move {job['display_id']} / {name_text(job['name'])} to pending order:", order=True), submit)

    def action_cancel(self):
        if self.external_read_only():
            return
        job = self.selected()
        if not job:
            return
        def submit(value):
            if value == 'CANCEL':
                self.apply(lambda: self.store.change(job['display_id'], 'cancel', confirm=True, expected=job['status']))
        self.push_screen(Prompt(f"Cancel {job['display_id']} / {name_text(job['name'])}?\nRunning cancellation terminates its managed process group. Dispatch pauses."), submit)
