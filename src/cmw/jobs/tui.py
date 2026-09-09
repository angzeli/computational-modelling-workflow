"""Observation-only Textual client; all controls revalidate in the queue store."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Label, Static

from .cli import elapsed, safe_text, tail
from .store import PENDING, JobsError, Store


def literal(value):
    return Text(safe_text(value))


def timestamp(value):
    return "—" if value is None else datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def detail(job, *, narrow=False):
    if not job:
        return "No selection. Add a prepared job through cmw jobs add."
    resource = job["resources"]
    resources = (f"Requested: {resource['cpus'] or '—'} CPUs / {resource['memory_gib'] or '—'} GiB\n"
                 f"MPI: {resource['mpi_ranks'] or '—'} ranks × {resource['threads_per_rank'] or '—'} threads/rank\n" if narrow else
                 f"CPUs requested: {resource['cpus'] or '—'}    MPI ranks: {resource['mpi_ranks'] or '—'}    "
                 f"Threads/rank: {resource['threads_per_rank'] or '—'}    RAM requested: {resource['memory_gib'] or '—'} GiB\n")
    return (f"SELECTED: {job['display_id']} / {job['name']}\n"
            f"Status: {job['status']}    Order: {job['order'] or '—'}    Engine: {job['engine']}\n"
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

    def __init__(self, store, job_id, logs=False):
        super().__init__()
        self.store, self.job_id, self.logs = store, job_id, logs
        self.log_stamps = None

    def compose(self):
        with Vertical():
            yield Label("LOG TAIL · stdout / stderr · bounded to 16 KiB each" if self.logs else "ATTEMPT DETAILS")
            with VerticalScroll():
                yield Static(id="body", markup=False)
            yield Label("Esc / Q: back · PgUp / PgDn: scroll")

    def on_mount(self):
        self.refresh_content()
        if self.logs:
            self.set_interval(1, self.refresh_content)

    def refresh_content(self):
        job = next((j for j in self.store.snapshot()["jobs"] if j["display_id"] == self.job_id), None)
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
            text = (detail(job) + f"\n\nAttempt: {job['attempt_id']}\n"
                    f"Enqueued: {timestamp(job['enqueued_at'])}\nStarted: {timestamp(job['started_at'])}\n"
                    f"Finished: {timestamp(job['finished_at'])}\nExit code: {job['exit_code']}   Signal: {job['signal']}\n"
                    f"Failure policy: {job['on_failure']}\nCHECK: Not evaluated\n\n"
                    + json.dumps({key: job[key] for key in ("argv", "env", "cwd", "logs", "worker", "group", "layout")}, indent=2, ensure_ascii=False))
        self.query_one("#body", Static).update(literal(text))


class JobsTable(DataTable):
    BINDINGS = [Binding("enter", "select_cursor", "Details")]


class JobsApp(App):
    TITLE = "CMW / JOBS"
    CSS = """
    Screen { background: #111820; color: #d7e0e8; }
    #heading { height: 1; padding: 0 1; color: #b7d8dd; text-style: bold; }
    #controller { height: auto; min-height: 2; padding: 0 1; color: #a9b9c7; }
    #table { height: 1fr; min-height: 5; margin: 1 1 0 1; }
    DataTable > .datatable--header { background: #22303d; color: #d7e0e8; text-style: bold; }
    DataTable > .datatable--cursor { background: #30475a; color: #ffffff; }
    #selected { height: auto; max-height: 7; margin: 1 1 0 1; padding: 0 1; border-left: solid #5c8996; }
    #events { height: 5; margin: 1 1 0 1; color: #9caeba; }
    #message { height: auto; max-height: 3; padding: 0 1; color: #efbd82; }
    Footer { background: #22303d; }
    #shortcuts { height: auto; background: #22303d; color: #d7e0e8; padding: 0 1; }
    """
    BINDINGS = [Binding("enter", "details", "Details"), Binding("l", "logs", "Logs"),
                Binding("p", "dispatch", "Dispatch"), Binding("h", "hold", "Hold"),
                Binding("o", "order", "Order"), Binding("x", "cancel", "Cancel"),
                Binding("q", "quit", "Detach", priority=True), Binding("ctrl+c", "quit", "Detach", show=False, priority=True)]

    def __init__(self, store=None, *, clock=None):
        super().__init__()
        self.store = store or Store()
        self.clock = clock
        self.state = None
        self.selected_id = None
        self.columns_mode = None
        self.row_ids = []

    def compose(self):
        yield Static("CMW / JOBS                                             LOCAL", id="heading")
        yield Static(id="controller", markup=False)
        yield JobsTable(id="table", cursor_type="row", zebra_stripes=True)
        yield Static(id="selected", markup=False)
        yield Static(id="events", markup=False)
        yield Static("Q / Ctrl-C detaches; execution continues independently.", id="message", markup=False)
        yield Static("Enter Details · L Logs · P Dispatch · H Hold\nO Order · X Cancel · Q / Ctrl-C Detach", id="shortcuts", markup=False)
        yield Footer()

    def on_mount(self):
        self.refresh_state()
        self.query_one(DataTable).focus()
        self.set_interval(0.5, self.refresh_state)

    def on_resize(self):
        if self.is_mounted:
            self.call_after_refresh(self.refresh_state)

    def selected(self):
        return next((j for j in (self.state or {}).get("jobs", []) if j["display_id"] == self.selected_id), None)

    def refresh_state(self):
        try:
            self.state = self.store.snapshot(now=self.clock() if self.clock else None)
        except (ValueError, OSError) as exc:
            self.query_one("#message", Static).update(literal(f"State unavailable: {exc}"))
            return
        control = self.state["controller"]
        status = "STALE — needs attention" if control["stale"] else "Online" if control["online"] else "Offline"
        jobs = self.state["jobs"]
        active = [j for j in jobs if j["status"] in {"Starting", "Run", "Cancelling", "Unknown"}]
        cpu = sum(j["resources"]["cpus"] or 0 for j in active) or "—"
        ram = sum(j["resources"]["memory_gib"] or 0 for j in active) or "—"
        age = "never" if control["age_seconds"] is None else f"{int(control['age_seconds'])}s ago"
        self.query_one("#controller", Static).update(literal(
            f"Controller: {status}   Dispatch: {'ON' if control['dispatch'] else 'OFF'}   Mode: Sequential   Updated: {age}\n"
            f"CPUs requested: {cpu}   RAM requested: {ram} GiB   Run: {sum(j['status']=='Run' for j in jobs)}   "
            f"Queue: {sum(j['status']=='Queue' for j in jobs)}   Hold: {sum(j['status']=='Hold' for j in jobs)}"))
        table = self.query_one(DataTable)
        wide = self.size.width >= 110
        self.query_one(Footer).display = wide
        self.query_one("#shortcuts", Static).display = not wide
        columns = ([('order', 'ORDER', 5), ('id', 'JOB ID', 9), ('name', 'NAME', 18), ('engine', 'ENGINE', 9),
                    ('status', 'STATUS', 10), ('cpus', 'CPUS', 4), ('elapsed', 'ELAPSED', 10), ('reason', 'REASON', 28)] if wide else
                   [('order', 'ORDER', 5), ('id', 'JOB ID', 8), ('status', 'STATUS', 10), ('cpus', 'CPUS', 4), ('elapsed', 'ELAPSED', 10)])
        # Rebuild only on layout changes; refreshes update cells in place.
        if wide != self.columns_mode:
            table.clear(columns=True)
            for key, label, width in columns:
                table.add_column(label, key=key, width=width)
            self.columns_mode = wide
            self.row_ids = []
        for job in jobs:
            key = job['display_id']
            values = {'order': str(job['order'] or '—').rjust(5), 'id': key, 'name': job['name'], 'engine': job['engine'],
                      'status': job['status'], 'cpus': str(job['resources']['cpus'] or '—').rjust(4),
                      'elapsed': elapsed(job['elapsed']).rjust(10), 'reason': job['reason'] or '—'}
            cells = []
            for column, _, _ in columns:
                cell = literal(values[column])
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
        self.query_one("#selected", Static).update(literal(detail(self.selected(), narrow=self.size.width < 110)))
        events = self.state['events'][-3:]
        self.query_one("#events", Static).update(literal("EVENTS\n" + "\n".join(
            f"{datetime.fromtimestamp(e['time']).strftime('%H:%M:%S')}  {'J'+str(e['job_id'])+'.1' if e['job_id'] else 'Controller'}  {e['message']}" for e in events)))
        if not jobs:
            self.query_one("#message", Static).update("No jobs. Use cmw jobs add, then start explicitly. Q detaches.")

    def on_data_table_row_selected(self, event):
        self.action_details()

    def on_data_table_row_highlighted(self, event):
        self.selected_id = event.row_key.value
        self.query_one("#selected", Static).update(literal(detail(self.selected(), narrow=self.size.width < 110)))

    def apply(self, function):
        try:
            function()
            self.query_one("#message", Static).update("Operation accepted.")
        except (ValueError, OSError) as exc:
            self.query_one("#message", Static).update(literal(f"Not applied: {exc}"))
        self.refresh_state()

    def action_details(self):
        if self.selected_id:
            self.push_screen(Inspect(self.store, self.selected_id))

    def action_logs(self):
        if self.selected_id:
            self.push_screen(Inspect(self.store, self.selected_id, logs=True))

    def action_dispatch(self):
        self.apply(lambda: self.store.dispatch(not self.state['controller']['dispatch']))

    def action_hold(self):
        job = self.selected()
        if job:
            self.apply(lambda: self.store.change(job['display_id'], 'release' if job['status']=='Hold' else 'hold', expected=job['status']))

    def action_order(self):
        job = self.selected()
        if not job:
            return
        if job['status'] not in PENDING:
            self.query_one("#message", Static).update("Only pending jobs can be reordered.")
            return
        def submit(value):
            if value is not None:
                self.apply(lambda: self.store.change(job['display_id'], 'move', position=int(value), expected=job['status']))
        self.push_screen(Prompt(f"Move {job['display_id']} / {job['name']} to pending order:", order=True), submit)

    def action_cancel(self):
        job = self.selected()
        if not job:
            return
        def submit(value):
            if value == 'CANCEL':
                self.apply(lambda: self.store.change(job['display_id'], 'cancel', confirm=True, expected=job['status']))
        self.push_screen(Prompt(f"Cancel {job['display_id']} / {job['name']}?\nRunning cancellation terminates its managed process group. Dispatch pauses."), submit)
