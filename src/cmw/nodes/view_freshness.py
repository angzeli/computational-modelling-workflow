"""Frontend receipts for node data displayed in live and frozen portal screens."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from textual.widgets import Static


@dataclass(frozen=True)
class ViewReceipt:
    monotonic: float
    wall: float
    generation: int


class NodeViews:
    def receipt_label(self, owner, receipt):
        if receipt is None:
            return f'{owner}: awaiting owner data; no receipt'
        timestamp = datetime.fromtimestamp(receipt.wall, timezone.utc).strftime('%H:%M:%S UTC')
        age = max(0., self.clock() - receipt.monotonic)
        return f'{owner}: Last reported · received {timestamp} · {age:.0f}s ago'

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
        screen.node_view_refresh = {}
        return screen

    def view_received(self, screen, owner, receipt):
        """Call only when accepted data is delivered to this particular screen."""
        if owner not in getattr(screen, 'node_view_receipts', {}):
            return
        screen.node_view_receipts[owner] = receipt
        self.sync_node_view(screen)

    def view_commands_allowed(self, screen):
        return all(self.receipt_current(owner, receipt)
                   for owner, receipt in getattr(screen, 'node_view_receipts', {}).items())

    def push_screen(self, screen, callback=None, wait_for_dismiss=False):
        if self.screen_stack and not hasattr(screen, 'node_view_receipts'):
            parent = getattr(self.screen, 'node_view_receipts', None)
            if parent is not None:
                self.bind_node_view(screen, parent, received=parent)
        mounted = super().push_screen(screen, callback, wait_for_dismiss)
        if hasattr(screen, 'node_view_receipts'):
            screen.call_after_refresh(self.sync_node_view, screen)
        return mounted

    def sync_node_view(self, screen):
        receipts = getattr(screen, 'node_view_receipts', None)
        if receipts is None or not screen.is_mounted:
            return
        stale = [(owner, receipt) for owner, receipt in receipts.items()
                 if not self.receipt_current(owner, receipt)]
        allowed = not stale
        reason = '\n'.join(self.receipt_label(owner, receipt) for owner, receipt in stale)
        if stale:
            reason += ' · Commands for the listed owners disabled until this view receives fresh data.'
        banner = getattr(screen, 'node_receipt_banner', None)
        if banner is None:
            banner = screen.node_receipt_banner = Static(markup=False, classes='node-receipt-banner')
            screen.mount(banner)
        if getattr(screen, 'node_receipt_reason', None) != reason:
            screen.node_receipt_reason = reason
            banner.update(reason)
            banner.display = bool(reason)
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
