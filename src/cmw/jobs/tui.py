"""Observation-only Textual client; all controls revalidate in the queue store."""
from __future__ import annotations

import json
import time
import unicodedata
from copy import deepcopy
from datetime import datetime, timezone
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
    """Keep identification stable across lifecycle changes; scroll below 52 columns."""
    columns = [('id', 'ID P/A' if bounded and viewport_width < 128 else 'JOB ID', 8),
               ('name', 'NAME', 12), ('status', 'STATUS', 10), ('elapsed', 'ELAPSED', 10)]
    if viewport_width >= 108:
        columns[-1:-1] = [('cpu_now', 'CPU NOW', 7), ('ram_now', 'RAM NOW', 15)]
    if viewport_width >= 128:
        columns.insert(0, ('order', 'ORDER', 5))
        columns.insert(3, ('role', 'ROLE', 9) if bounded else ('engine', 'ENGINE', 7))
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
            f"Status: {job['status']}    Order: {job['order'] or '—'}    Engine: {job['engine']}\n"
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

    def __init__(self, store, job_id, logs=False, job_provider=None):
        super().__init__()
        self.store, self.job_id, self.logs = store, job_id, logs
        self.log_stamps = None
        self.job_provider = job_provider

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
        text = (f"Mode: {state.get('mode', 'Sequential')}\n" + sharing_text(state) + "\n" + guard_text(state)
                + "\n\nRequested resources, admission commitments and observed usage are separate.\n"
                + "Declarations are not OS-enforced limits; external processes remain read-only.\n\n"
                + json.dumps({'scheduler': state.get('scheduler'), 'admission': state.get('admission')}, indent=2, ensure_ascii=False))
        self.query_one("#sharing-body", Static).update(literal(text))


class JobsTable(DataTable):
    BINDINGS = [Binding("enter", "select_cursor", "Details")]

    def on_focus(self):
        if self.id == "external" and self.row_count:
            self.app.external_selected_id = self.app.external_rows[self.cursor_row]
        elif self.id == "table":
            self.app.external_selected_id = None
        self.app.query_one("#selected", Static).update(literal(self.app.selected_text()))


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
    #events { height: 5; margin: 1 1 0 1; color: #9caeba; }
    #message { height: auto; max-height: 3; padding: 0 1; color: #efbd82; }
    Footer { background: #22303d; }
    #shortcuts { height: auto; background: #22303d; color: #d7e0e8; padding: 0 1; }
    """
    BINDINGS = [Binding("enter", "details", "Details"), Binding("n", "note", "Note"), Binding("l", "logs", "Logs"), Binding("b", "scheduling", "Scheduling"),
                Binding("p", "dispatch", "Dispatch"), Binding("h", "hold", "Hold"),
                Binding("o", "order", "Order"), Binding("x", "cancel", "Cancel"),
                Binding("q", "quit", "Detach", priority=True), Binding("ctrl+c", "quit", "Detach", show=False, priority=True)]

    def __init__(self, store=None, *, clock=None, observer=None, sampler=None):
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
        yield Static(id="events", markup=False)
        yield Static("Best-effort guard; no machine-wide reservation. Q / Ctrl-C detaches; execution continues.", id="message", markup=False)
        yield Static("Enter Details · N Note · L Logs · P Dispatch · H Hold\nO Order · X Cancel · B Scheduling · Q / Ctrl-C Detach", id="shortcuts", markup=False)
        yield Footer()

    def on_mount(self):
        self.refresh_state()
        self.query_one(DataTable).focus()
        self.set_interval(0.5, self.refresh_state)
        self.collect_activity()
        self.set_interval(2, self.collect_activity)

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
        prefix = f"SELECTED: {job['display_id']}{role} / "
        heading = prefix + table_name(job['name'], max(1, width - cell_len(prefix))).plain
        lines = detail(job, narrow=self.size.width < 110).splitlines()
        lines[0] = heading
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
        self.query_one("#guard", Static).update(literal(guard_text(display_state)))
        compact = self.size.width < 110 or self.size.height < 36
        sharing = self.query_one("#sharing", Static)
        sharing.display = self.state.get('mode') == 'Bounded Sharing'
        sharing.styles.max_height = 1 if compact else 4
        sharing.update(literal(sharing_text(display_state, compact=compact)))
        table = self.query_one("#external", DataTable)
        observations = (guard or {}).get("observations", [])
        self.query_one("#external-heading", Static).update(literal(
            f"EXTERNAL ACTIVITY — READ ONLY · {len(observations)} observations · {len((guard or {}).get('coverage', {}).get('warnings', []))} coverage warnings"))
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
        if self.is_mounted:
            self.call_after_refresh(self.refresh_state)

    def selected(self):
        return next((j for j in (self.state or {}).get("jobs", []) if j["display_id"] == self.selected_id), None)

    def refresh_state(self):
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
        active = [j for j in jobs if j["status"] in {"Starting", "Run", "Cancelling", "Unknown"}]
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
        self.query_one("#guard", Static).styles.max_height = 3 if compact else 5
        self.query_one("#external", DataTable).styles.height = 4 if compact else 5
        self.query_one("#selected", Static).styles.max_height = 3 if compact else 7
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
            values = {'order': str(job['order'] or '—').rjust(5), 'id': key + (' A' if role == 'auxiliary' else ' P') if bounded and viewport_width < 128 else key, 'name': job['name'], 'engine': job['engine'],
                      'role': role.title(),
                      'status': job['status'], 'cpus': str(job['resources']['cpus'] or '—').rjust(4),
                      'cpu_now': cpu_now, 'ram_now': ram_now,
                      'elapsed': elapsed(job['elapsed']).rjust(10), 'reason': job['reason'] or '—'}
            cells = []
            for column, _, width in columns:
                cell = table_name(values[column], width) if column == 'name' else literal(values[column])
                if column == 'status':
                    cell.stylize({'Run':'#86c6b0', 'Queue':'#b9c8dc', 'Hold':'#d9bc7e', 'Fail':'#ea9595',
                                  'Unknown':'bold #f3b875', 'Cancelling':'#e9ba94', 'Done':'#8baaa0'}.get(job['status'], '#a1afbc'))
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
            self.query_one("#message", Static).update("No managed jobs. Add, then start explicitly. Best-effort guard; no machine-wide reservation. Q detaches.")
            self.empty_notice_shown = True
        elif jobs:
            self.empty_notice_shown = False

    def on_data_table_row_selected(self, event):
        if event.data_table.id == "external":
            self.external_selected_id = event.row_key.value
        else:
            self.external_selected_id = None
            self.selected_id = event.row_key.value
        self.action_details()

    def on_data_table_row_highlighted(self, event):
        if event.data_table.id == "external":
            if not event.data_table.has_focus:
                return
            self.external_selected_id = event.row_key.value
        else:
            if self.query_one("#external", DataTable).has_focus:
                return
            self.external_selected_id = None
            self.selected_id = event.row_key.value
        self.query_one("#selected", Static).update(literal(self.selected_text()))

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
        if isinstance(self.screen, NoteEditor) and action in {
            'details', 'note', 'logs', 'scheduling', 'dispatch', 'hold', 'order', 'cancel', 'quit'
        }:
            return False
        return super().check_action(action, parameters)

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

    def action_details(self):
        if self.external_selected():
            self.push_screen(ExternalInspect(self.external_selected(), provider=self.external_observation))
            return
        if self.selected_id:
            self.push_screen(Inspect(self.store, self.selected_id, job_provider=self.current_job))

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
