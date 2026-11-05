"""Fit a frontend receipt and view title into a known number of terminal cells."""
from dataclasses import dataclass

from .formatting import cell_width, fit_text


@dataclass(frozen=True)
class ReceiptCaption:
    owner: str
    timestamp: str | None
    age: str | None

    def format(self, title: str, width: int) -> str:
        """Keep the evidence marker even when identifying details must be dropped."""
        marker = 'Last reported' if self.timestamp is not None else 'Last reported · receipt unknown'
        separator = ' · '
        stamp = f'received {self.timestamp}' if self.timestamp is not None else ''
        for fields in ((stamp, self.age), (self.age,), ()):
            evidence = separator.join(part for part in (marker, *fields) if part)
            prefix = self.owner + ': ' + evidence
            remaining = width - cell_width(prefix)
            if remaining >= 0:
                if title and remaining > cell_width(separator):
                    return prefix + separator + fit_text(title, remaining - cell_width(separator))
                return prefix
            # Once a field is dropped, a lower-priority title must stay dropped.
            title = ''
        owner_width = width - cell_width(marker) - 2
        if owner_width > 0:
            return fit_text(self.owner, owner_width) + ': ' + marker
        if cell_width(marker) <= width:
            return marker
        if width >= len('Last reported'):
            return 'Last reported'
        # An undersized terminal or a frame still being laid out can be narrower.
        return '~' if width > 0 else ''
