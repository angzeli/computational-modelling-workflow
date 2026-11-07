"""Frontend receipts for node data displayed in live and frozen portal screens."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from rich.text import Text
from textual.containers import Vertical

from cmw.progress.receipt import ReceiptCaption


@dataclass(frozen=True)
class ViewReceipt:
    monotonic: float
    wall: float
    generation: int


class NodeViews:
    def receipt_caption(self, owner, receipt):
        if receipt is None:
            return ReceiptCaption(owner, None, None)
        timestamp = datetime.fromtimestamp(receipt.wall, timezone.utc).strftime('%H:%M:%S UTC')
        age = max(0., self.clock() - receipt.monotonic)
        return ReceiptCaption(owner, timestamp, f'{age:.0f}s ago')

    def receipt_label(self, owner, receipt, width=None):
        width = self.size.width if width is None else width
        return self.receipt_caption(owner, receipt).format('', width)

    def view_receipt(self, owner, operation, target=None):
        lane = self.lanes[owner]
        if target is None:
            return lane.receipts.get(operation)
        key = (tuple(target[name] for name in ('node_id', 'queue_id', 'job_id', 'attempt_id'))
               if isinstance(target, dict) else target)
        return lane.cache.get(key, {}).get(operation + '.receipt')

    def receipt_current(self, owner, receipt):
        lane = self.lanes[owner]
        return (receipt is not None and receipt.generation == lane.observation_generation
                and lane.fresh(self.clock()))

    def bind_node_view(self, screen, owner, *, received=None):
        if isinstance(owner, str):
            screen.node_view_receipts = {owner: received}
        else:
            screen.node_view_receipts = {name: (received or {}).get(name) for name in owner}
        screen.node_view_active = True
        screen.node_view_refresh = {}
        return screen

    def view_received(self, screen, owner, receipt):
        """Call only when accepted data is delivered to this particular screen."""
        if (screen not in self.screen_stack or not getattr(screen, 'node_view_active', False)
                or owner not in getattr(screen, 'node_view_receipts', {})):
            return
        screen.node_view_receipts[owner] = receipt
        self.sync_node_view(screen)

    def view_commands_allowed(self, screen):
        receipts = getattr(screen, 'node_view_receipts', {})
        return (not self.closed and screen in self.screen_stack and bool(receipts)
                and getattr(screen, 'node_view_active', False)
                and all(self.receipt_current(owner, receipt) for owner, receipt in receipts.items()))

    def push_screen(self, screen, callback=None, wait_for_dismiss=False):
        if self.screen_stack and not hasattr(screen, 'node_view_receipts'):
            parent = getattr(self.screen, 'node_view_receipts', None)
            if parent is not None:
                self.bind_node_view(screen, parent, received=parent)
        mounted = super().push_screen(screen, callback, wait_for_dismiss)
        if hasattr(screen, 'node_view_receipts'):
            screen.call_after_refresh(self.sync_node_view, screen)
        return mounted

    def unbind_node_view(self, screen):
        # Stop lane updates immediately; retain the captured receipts because a
        # dismiss callback can still validate the user's already chosen action.
        if hasattr(screen, 'node_view_receipts'):
            screen.node_view_active = False
            screen.node_view_refresh = {}

    def pop_screen(self):
        screen = self.screen
        result = super().pop_screen()
        self.unbind_node_view(screen)
        return result

    def sync_node_view(self, screen):
        receipts = getattr(screen, 'node_view_receipts', None)
        if (receipts is None or not screen.is_mounted or screen not in self.screen_stack
                or not getattr(screen, 'node_view_active', False)):
            return
        stale = [(owner, receipt) for owner, receipt in receipts.items()
                 if not self.receipt_current(owner, receipt)]
        allowed = not stale
        frame = getattr(screen, 'node_view_frame', None)
        if frame is None:
            # The bound dialog's existing outer panel owns all four edges.
            frames = screen.query(Vertical)
            if not frames:
                return  # The panel has already left a partially unmounted screen.
            frame = screen.node_view_frame = frames.first()
            frame.add_class('node-view-frame')
        elif not frame.is_attached:
            return
        # Textual reserves the corners plus two cells on each end of a border title.
        width = max(1, (frame.region.width or self.size.width) - 6)
        captions = tuple(self.receipt_label(owner, receipt, width) for owner, receipt in stale)
        if getattr(screen, 'node_frame_captions', None) != captions:
            screen.node_frame_captions = captions
            frame.border_title = Text(captions[0]) if captions else None
            frame.border_subtitle = 'Owner commands disabled until fresh data' if captions else None
            # Multi-owner Search lists the remaining receipts in its coverage pane.
            other_receipts = getattr(screen, 'show_other_receipts', None)
            if other_receipts is not None:
                other_receipts(captions[1:])
        stale_owners = frozenset(owner for owner, _ in stale)
        if getattr(screen, 'node_stale_owners', None) != stale_owners:
            screen.node_stale_owners = stale_owners
            changed = getattr(screen, 'node_commands_changed', None)
            if changed is not None:
                changed(allowed)

    def sync_node_views(self):
        for screen in self.screen_stack:
            self.sync_node_view(screen)

    def refresh_recovered_views(self, owner):
        lane = self.lanes[owner]
        for screen in self.screen_stack:
            receipts = getattr(screen, 'node_view_receipts', {})
            if (owner in receipts and not self.receipt_current(owner, receipts[owner])
                    and screen.node_view_refresh.get(owner) != lane.observation_generation):
                refresh = getattr(screen, 'refresh_node_observation', None)
                if refresh is not None and screen.is_mounted:
                    screen.node_view_refresh[owner] = lane.observation_generation
                    refresh()
