"""One-shot, identity-aware view restoration across modal navigation."""
from __future__ import annotations

from dataclasses import dataclass
from weakref import WeakKeyDictionary

from textual import events
from textual.app import App
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import DataTable


@dataclass(frozen=True)
class ScrollSnapshot:
    widget: Widget
    x: float
    y: float
    target_x: float
    target_y: float


@dataclass(frozen=True)
class TableSnapshot:
    table: DataTable
    rows: tuple
    cursor_identity: object
    column_key: object
    column_index: int
    anchor_identity: object
    anchor_offset: float
    cursor_relative_y: float | None
    visible_height: int
    cursor_visible: bool
    scroll: ScrollSnapshot


@dataclass(frozen=True)
class ViewSnapshot:
    screen: Screen
    context: object
    selection: object
    input_generation: int
    focus: Widget | None
    tables: tuple[TableSnapshot, ...]
    scrolls: tuple[ScrollSnapshot, ...]


@dataclass
class _ReturnFrame:
    view: ViewSnapshot
    child: Screen


@dataclass
class _Restore:
    view: ViewSnapshot
    remaining_tables: int = 0


class NavigationApp(App):
    """Preserve each actual parent view without changing modal result semantics.

    Domain apps supply immutable row/selection identities through the four hooks
    below. These snapshots are transient UI state, never execution authority.
    """

    DEFAULT_CSS = """
    DataTable.navigation-unavailable > .datatable--cursor,
    DataTable.navigation-unavailable:focus > .datatable--cursor {
        background: transparent; color: $text-muted; text-style: none;
    }
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._navigation_frames: list[_ReturnFrame] = []
        self._navigation_inputs = WeakKeyDictionary()
        self._navigation_restores = WeakKeyDictionary()

    def navigation_context(self, screen):
        return (screen, getattr(screen, 'selection', None))

    def navigation_row_identity(self, table, key):
        return key

    def navigation_selection(self, screen):
        return None

    def navigation_restore_selection(self, screen, snapshot):
        """Return False to refuse cursor restoration to an unavailable target."""
        return True

    @staticmethod
    def _scroll_snapshot(widget):
        return ScrollSnapshot(widget, widget.scroll_x, widget.scroll_y,
                              widget.scroll_target_x, widget.scroll_target_y)

    def _table_rows(self, table):
        """Return immutable identities and row positions using public table APIs."""
        rows = []
        y = table.header_height if table.show_header else 0
        for row in table.ordered_rows:
            height = table.get_row_height(row.key)
            rows.append((self.navigation_row_identity(table, row.key.value), y, height))
            y += height
        return rows

    def capture_view(self, screen=None):
        screen = self.screen if screen is None else screen
        tables, scrolls = [], []
        for widget in screen.walk_children(with_self=True):
            if not isinstance(widget, Widget):
                continue
            if isinstance(widget, DataTable):
                rows = self._table_rows(widget)
                cursor = rows[widget.cursor_row] if 0 <= widget.cursor_row < len(rows) else None
                columns = widget.ordered_columns
                column = columns[widget.cursor_column].key.value if 0 <= widget.cursor_column < len(columns) else None
                fixed_height = (widget.header_height if widget.show_header else 0)
                fixed_height += sum(row[2] for row in rows[:widget.fixed_rows])
                visible_top = widget.scroll_y + fixed_height
                anchor = next((row for row in rows[widget.fixed_rows:] if row[1] + row[2] > visible_top), None)
                tables.append(TableSnapshot(
                    widget, tuple(row[0] for row in rows), cursor[0] if cursor else None,
                    column, widget.cursor_column, anchor[0] if anchor else None,
                    visible_top - anchor[1] if anchor else 0,
                    cursor[1] - widget.scroll_y if cursor else None,
                    widget.scrollable_content_region.height,
                    bool(cursor and cursor[1] - widget.scroll_y >= fixed_height
                         and cursor[1] + cursor[2] - widget.scroll_y <= widget.scrollable_content_region.height),
                    self._scroll_snapshot(widget)))
            elif widget.is_scrollable or widget.scroll_x or widget.scroll_y:
                scrolls.append(self._scroll_snapshot(widget))
        return ViewSnapshot(screen, self.navigation_context(screen),
                            self.navigation_selection(screen),
                            self._navigation_inputs.get(screen, 0), screen.focused,
                            tuple(tables), tuple(scrolls))

    def navigation_pending(self, screen=None):
        screen = self.screen if screen is None else screen
        return screen in self._navigation_restores

    def navigation_origin_covered(self, screen=None):
        screen = self.screen if screen is None else screen
        return any(frame.view.screen is screen for frame in self._navigation_frames)

    def push_screen(self, screen, callback=None, wait_for_dismiss=False):
        view = self.capture_view() if self.screen_stack else None
        if view is not None:
            self._navigation_restores.pop(view.screen, None)
        result = super().push_screen(screen, callback, wait_for_dismiss)
        if view is not None:
            self._navigation_frames.append(_ReturnFrame(view, self.screen))
        return result

    def pop_screen(self):
        child = self.screen
        result = super().pop_screen()
        if self._navigation_frames and self._navigation_frames[-1].child is child:
            frame = self._navigation_frames.pop()
            if frame.view.screen is self.screen:
                self.restore_view(frame.view)
        return result

    async def on_event(self, event):
        # Observe input before widgets consume arrows, scrolling, or priority keys.
        # The key dismissing a child belongs to that child, not its resumed parent.
        if (isinstance(event, (events.Key, events.MouseDown,
                               events.MouseScrollUp, events.MouseScrollDown))
                and not event.is_forwarded and self.screen_stack):
            screen = self.screen
            self._navigation_inputs[screen] = self._navigation_inputs.get(screen, 0) + 1
            self._navigation_restores.pop(screen, None)
        await super().on_event(event)

    def restore_view(self, snapshot):
        if snapshot.screen is not self.screen:
            return
        restore = _Restore(snapshot)
        self._navigation_restores[snapshot.screen] = restore
        self.call_after_refresh(self._restore_cursor, restore)

    def _restore_valid(self, restore):
        view = restore.view
        return (self._navigation_restores.get(view.screen) is restore
                and view.screen is self.screen and view.screen.is_attached
                and self.navigation_context(view.screen) == view.context
                and self._navigation_inputs.get(view.screen, 0) == view.input_generation)

    def _discard_restore(self, restore):
        screen = restore.view.screen
        if self._navigation_restores.get(screen) is restore:
            self._navigation_restores.pop(screen, None)

    @staticmethod
    def _unique_row(rows, identity):
        if identity is None:
            return None
        matches = [index for index, row in enumerate(rows) if row[0] == identity]
        return matches[0] if len(matches) == 1 else None

    def _restore_cursor(self, restore):
        if not self._restore_valid(restore):
            self._discard_restore(restore)
            return
        view = restore.view
        allow_cursor = self.navigation_restore_selection(view.screen, view) is not False
        for saved in view.tables:
            table = saved.table
            if not table.is_attached or table.screen is not view.screen:
                continue
            rows = self._table_rows(table)
            index = self._unique_row(rows, saved.cursor_identity)
            if not allow_cursor or index is None:
                continue
            columns = [column.key.value for column in table.ordered_columns]
            column = columns.index(saved.column_key) if saved.column_key in columns else min(saved.column_index, max(0, len(columns) - 1))
            if (table.cursor_row, table.cursor_column) != (index, column):
                # Suppress only this programmatic cursor repair's notifications.
                with table.prevent(DataTable.RowHighlighted, DataTable.CellHighlighted,
                                   DataTable.ColumnHighlighted):
                    table.move_cursor(row=index, column=column, animate=False, scroll=False)
        if view.focus is not None and view.focus.is_attached and view.focus.screen is view.screen:
            view.screen.set_focus(view.focus, scroll_visible=False)
        # Drain each table's queue: cursor watchers post their scroll callbacks
        # there, whereas an app callback could otherwise overtake that message.
        tables = [saved.table for saved in view.tables
                  if saved.table.is_attached and saved.table.screen is view.screen]
        restore.remaining_tables = len(tables)
        if not tables:
            self.call_after_refresh(self._restore_offsets, restore)
        for table in tables:
            if not table.call_after_refresh(self._cursor_drained, restore):
                self._discard_restore(restore)

    def _cursor_drained(self, restore):
        if not self._restore_valid(restore):
            self._discard_restore(restore)
            return
        restore.remaining_tables -= 1
        if restore.remaining_tables == 0:
            self.call_after_refresh(self._restore_offsets, restore)

    def _restore_offsets(self, restore):
        if not self._restore_valid(restore):
            self._discard_restore(restore)
            return
        view = restore.view
        for saved in view.tables:
            table = saved.table
            if not table.is_attached or table.screen is not view.screen:
                continue
            rows = self._table_rows(table)
            y = saved.scroll.y
            if tuple(row[0] for row in rows) != saved.rows:
                anchor = self._unique_row(rows, saved.anchor_identity)
                cursor = self._unique_row(rows, saved.cursor_identity)
                if anchor is not None:
                    fixed_height = table.header_height if table.show_header else 0
                    fixed_height += sum(row[2] for row in rows[:table.fixed_rows])
                    y = rows[anchor][1] + saved.anchor_offset - fixed_height
                elif cursor is not None and saved.cursor_relative_y is not None:
                    y = rows[cursor][1] - saved.cursor_relative_y
            height = table.scrollable_content_region.height
            cursor = self._unique_row(rows, saved.cursor_identity)
            if (height < saved.visible_height and saved.cursor_visible
                    and cursor is not None and cursor >= table.fixed_rows):
                fixed_height = table.header_height if table.show_header else 0
                fixed_height += sum(row[2] for row in rows[:table.fixed_rows])
                row_y, row_height = rows[cursor][1:]
                # Preserve the anchor unless a smaller viewport would hide a
                # previously visible selection; move only enough to retain it.
                y = min(y, row_y - fixed_height)
                y = max(y, row_y + row_height - height)
            table.scroll_to(x=saved.scroll.x, y=y, animate=False, force=True, immediate=True)
        for saved in view.scrolls:
            widget = saved.widget
            if widget.is_attached and widget.screen is view.screen:
                widget.scroll_to(x=saved.x, y=saved.y, animate=False, force=True, immediate=True)
        self._discard_restore(restore)
