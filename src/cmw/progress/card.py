"""Cell-aware scientific cards driven only by adapter presentation descriptors."""
from __future__ import annotations

import math

from .formatting import age_text, cell_width, context_text, fit_text, value_text


def _metric(cell, snapshot):
    by_id = {item.latest.semantic_id: item.latest for item in snapshot.metrics}
    return next((by_id[key] for key in cell.metric_ids if key in by_id), None)


def _reference(cell, metric):
    if not cell.show_context or not metric.context:
        return ''
    labels = dict(cell.context_labels)
    levels = tuple(level for level in metric.context if not cell.context_kinds or level.kind in cell.context_kinds)
    return (' @' + '/'.join(labels.get(level.kind, level.kind) + str(level.index) for level in levels)) if levels else ''


def _provisional_target(metric, snapshot):
    return (metric.qualification == 'provisional' or 'PROVISIONAL' in (metric.relation or '')
            or metric.criterion is not None and any(setting.qualification == 'provisional'
                and setting.origin == metric.criterion.origin for setting in snapshot.settings))


def _scientific_value(metric, width):
    digits = 5
    if metric.value and metric.precision and math.isfinite(metric.precision) and metric.precision > 0:
        # Metric precision is the native half-last-place resolution.
        last_place = round(math.log10(metric.precision) + math.log10(2))
        digits = max(1, min(digits, math.floor(math.log10(abs(metric.value))) - last_place + 1))
    for significant in range(digits, 0, -1):
        mantissa, exponent = f'{metric.value:.{significant - 1}e}'.split('e')
        if '.' in mantissa:
            mantissa = mantissa.rstrip('0').rstrip('.')
        value = mantissa + 'e' + exponent
        if cell_width(value) <= width:
            return value
    return value


def _cell(cell, snapshot, width, *, main_card=False):
    if cell.kind == 'text':
        return fit_text(cell.text, width)
    if cell.kind == 'context':
        levels = tuple(level for level in snapshot.iteration if not cell.context_kinds or level.kind in cell.context_kinds)
        return fit_text(context_text(levels) if levels else cell.text or 'Context unavailable', width)
    metric = _metric(cell, snapshot)
    if cell.kind == 'target':
        if metric is None or metric.criterion is None:
            return fit_text(cell.text or 'Target unavailable', width)
        target = metric.criterion
        text = f'{cell.label or "Target"} {target.comparator}{target.value:g} {target.unit.replace("Angstrom", "Å")}'
        if _provisional_target(metric, snapshot):
            text += '*'
        if 'UNCERTAIN' in (metric.relation or ''):
            text += '?'
        if target.kind != 'engine-stopping':
            text += ' ' + target.kind
        return fit_text(text, width)
    if metric is None:
        return fit_text(cell.text or f'{cell.label or "Value"} —', width)
    label = cell.label or metric.label
    # Units and complete-row labels take precedence over verbose metric labels.
    suffix = value_text(metric, compact=True)
    # Preserve useful native digits when the complete value/unit/context fits.
    if main_card and cell.value_format == 'scientific' and metric.value is not None and math.isfinite(metric.value):
        unit = (' ' + metric.unit.replace('Angstrom', 'Å')) if metric.unit and metric.unit != 'native' else ''
        marker = '*' if metric.qualification == 'current-input-derived' else ''
        suffix = _scientific_value(metric, width - cell_width(label + ' ' + unit + _reference(cell, metric) + marker))
    elif metric.value is not None and math.isfinite(metric.value):
        precise = f'{metric.value:.6g}'
        unit = (' ' + metric.unit.replace('Angstrom', 'Å')) if metric.unit and metric.unit != 'native' else ''
        if cell_width(label + ' ' + precise + unit + _reference(cell, metric)) <= width:
            suffix = precise
    if metric.qualification == 'current-input-derived':
        suffix += '*'
    if metric.unit and metric.unit != 'native':
        unit = 'unit?' if metric.unit == 'unspecified' else metric.unit.replace('Angstrom', 'Å')
        suffix += ' ' + unit
    suffix += _reference(cell, metric)
    room = max(0, width - cell_width(suffix) - 1)
    label_width = min(room, max(8, cell_width(label)))
    shown_label = fit_text(label, label_width)
    text = shown_label + ' ' * (label_width - cell_width(shown_label) + 1) + suffix
    return fit_text(text, width)


def verification_summary(snapshot, *, width, now, compact=False):
    """Keep actual rows, correspondence and observation timestamps independent."""
    conflicts = [item for item in snapshot.verification if item.status == 'CONFLICT']
    primary = next((item for item in snapshot.verification if item.domain == 'electronic-row'), None)
    diagnostics = {item.role: item for item in snapshot.source_diagnostics}
    source = diagnostics.get(primary.source_role) if primary else None
    parts = []
    stamps = [item.latest.observed_at for item in snapshot.metrics if item.latest.observed_at is not None
              and (not primary or not item.latest.evidence or any(ref.source_role == primary.source_role for ref in item.latest.evidence))]
    stamp = max(stamps) if stamps else None
    if conflicts:
        pairs = '/'.join(dict.fromkeys(role for check in conflicts
                        for role in (check.source_role, check.counterpart_role) if role))
        parts += ['SOURCE CONFLICT', pairs]
        if not compact:
            parts.append('Enter Details')
    elif primary and primary.observed_frontier is not None:
        role, observed, checked = primary.source_role or 'source', primary.observed_frontier, primary.checked_frontier
        counterpart = primary.counterpart_role or 'counterpart'
        if source and source.status == 'NOT_YET_PARSED':
            parts += [role + ' catching up', f'{source.usable_rows} complete rows', f'latest @SCF{observed}' if source.usable_rows else f'retained @SCF{observed}']
        elif source and source.status in {'UNAVAILABLE', 'MALFORMED', 'UNSUPPORTED', 'UNRESOLVED'}:
            parts += [role + ' ' + source.status.lower(), f'cached SCF{observed}', 'Details']
        elif checked is None:
            parts += [f'{role} SCF {observed} source-local', counterpart + ' ' + primary.status.lower()]
        elif checked < observed:
            parts += ([f'{role} {observed} source-local', f'{counterpart} checked {checked}'] if compact else
                      [f'{role} SCF {observed}', f'{counterpart} checked {checked}', f'{observed} source-local'])
        else:
            parts += [role, f'{counterpart} matched through SCF {checked}']
        if source and source.latest_iteration == observed and source.last_complete_row_at is not None:
            stamp = source.last_complete_row_at
    elif diagnostics:
        labels = {'UNDECLARED': 'not declared', 'NOT_YET_PARSED': 'catching up'}
        parts += [item.role + ' ' + labels.get(item.status, item.status.lower()) for item in diagnostics.values()]
        parts.append('SCF values unavailable')
        stamp = None
    elif primary:
        parts += [(primary.source_role or 'source') + ' values unavailable',
                  (primary.counterpart_role or 'counterpart') + ' ' + primary.status.lower()]
    else:
        # Other adapters retain generic observation wording without VASP assumptions.
        parts.append('source observation')
        stamps = [item.latest.observed_at for item in snapshot.metrics if item.latest.observed_at is not None]
        stamp = max(stamps) if stamps else None
    if not conflicts and (snapshot.context.source_conflicts or snapshot.status in ('SOURCE_CONFLICT', 'SOURCE_UNRESOLVED', 'UNSUPPORTED')):
        parts.insert(0, snapshot.status if snapshot.status != 'OBSERVED' else 'SOURCE_CONFLICT')
    if snapshot.read_health not in ('OK', 'PENDING'):
        parts.insert(0, snapshot.read_health)
    if snapshot.completeness == 'CATCHING_UP' and not (source and source.status == 'NOT_YET_PARSED'):
        parts.insert(0, 'CATCHING UP')
    parts = list(dict.fromkeys(parts))
    age = 'data ' + age_text(stamp, now) if stamp is not None else None
    separator = ' · '
    if compact and cell_width(separator.join(parts + ([age] if age else []))) > width:
        separator = '; '
        if age and cell_width(separator.join(parts + [age])) > width:
            age = age.removesuffix(' ago')
    text = separator.join(parts)
    if age:
        return fit_text(text, max(0, width - cell_width(age) - len(separator))) + separator + age
    return fit_text(text, width)


def _frame(snapshot, *, width, now, heading, max_lines):
    """Use viewport/descriptor geometry only, so polling cannot shift columns."""
    rows = [(group.label if index == 0 else '', row)
            for group in snapshot.card_groups for index, row in enumerate(group.rows)]
    columns = max((len(row) for _, row in rows), default=0)
    if not columns or width < 54 or max_lines is not None and len(rows) + 2 > max_lines:
        return None
    frame_height = min(len(rows) + 4, max_lines) if max_lines is not None else len(rows) + 4
    label_width = max(cell_width(group.label) for group in snapshot.card_groups)
    if cell_width(heading) > width - 6:
        notices = [('settings differ' if part.startswith('settings differ:') else part)
                   .replace('; Details', '').replace('* provisional target', '* provisional')
                   .replace('* provisional settings', '* provisional')
                   for part in heading.split(' · ')]
        heading = ' · '.join(dict.fromkeys(notices))
    # Reclaim decoration in stages before returning to the original plain card.
    for padding, separator, minimum in ((1, '│', 32), (0, '│', 28)):
        available = width - 2 - columns - 2 * padding * (columns + 1) - label_width
        value_width, remainder = divmod(available, columns)
        title = (' ' if padding else '') + heading + (' ' if padding else '')
        if value_width < minimum or cell_width(title) + 1 > width - 2:
            continue
        widths = [label_width] + [value_width] * columns
        widths[-1] += remainder
        def line(values):
            return '│' + separator.join(' ' * padding + value + ' ' * (size - cell_width(value) + padding)
                                        for value, size in zip(values, widths)) + '│'
        lines = ['┌─' + title + '─' * (width - 3 - cell_width(title)) + '┐']
        for label, row in rows:
            values = [label] + [_cell(cell, snapshot, size, main_card=True) for cell, size in zip(row, widths[1:])]
            lines.append(line(values + [''] * (len(widths) - len(values))))
        if frame_height == len(rows) + 4:
            lines.append('├' + '┴'.join('─' * (size + 2 * padding) for size in widths) + '┤')
        if frame_height >= len(rows) + 3:
            footer_width = width - 2 - 2 * padding
            footer = verification_summary(snapshot, width=footer_width, now=now, compact=width < 90)
            lines.append('│' + ' ' * padding + footer + ' ' * (footer_width - cell_width(footer) + padding) + '│')
            lines.append('└' + '─' * (width - 2) + '┘')
        else:
            # Like the integrated title, the evidence label is part of the edge.
            footer = verification_summary(snapshot, width=width - 6, now=now, compact=True)
            lines.append('└─ ' + footer + ' ' + '─' * (width - 5 - cell_width(footer)) + '┘')
        return '\n'.join(lines)
    return None


def card_text(snapshot, *, width, now, max_lines=None, framed=False):
    """Render fixed logical groups; engine-specific meaning lives in their cells."""
    label_width = max((cell_width(group.label) for group in snapshot.card_groups), default=0)
    label_width = min(label_width, max(0, width // 4))
    body_width = max(1, width - label_width - 2)
    warnings = {event.event_id for event in snapshot.events
                if event.kind in ('native-fatal', 'native-failure', 'native-stop-request')}
    notices = []
    if warnings:
        notices.append(f'native warnings {len(warnings)}; Details')
    discrepancies = [setting.name for setting in snapshot.settings if setting.discrepancy]
    if discrepancies:
        notices.append('settings differ: ' + '/'.join(discrepancies))
    if any(item.latest.qualification == 'current-input-derived' for item in snapshot.metrics):
        notices.append('* current-input scope')
    if any(_provisional_target(item.latest, snapshot) for item in snapshot.metrics):
        notices.append('* provisional target' if any(item.latest.criterion for item in snapshot.metrics
                                                    if _provisional_target(item.latest, snapshot)) else '* provisional')
    if any(setting.qualification == 'provisional' for setting in snapshot.settings):
        notices.append('* provisional settings')
    if any('UNCERTAIN' in (item.latest.relation or '') for item in snapshot.metrics):
        notices.append('? uncertain')
    heading = ' · '.join(('SCIENTIFIC', *notices))
    if framed:
        panel = _frame(snapshot, width=width, now=now, heading=heading, max_lines=max_lines)
        if panel is not None:
            return panel
    lines = [heading]
    for group in snapshot.card_groups:
        for index, row in enumerate(group.rows):
            label = fit_text(group.label if index == 0 else '', label_width)
            widths = [max(1, (body_width - 2 * (len(row) - 1)) // max(1, len(row)))] * len(row)
            if widths:
                widths[-1] += max(0, body_width - sum(widths) - 2 * (len(row) - 1))
            values = [_cell(cell, snapshot, size, main_card=framed) for cell, size in zip(row, widths)]
            body = '  '.join(value + ' ' * max(0, size - cell_width(value))
                             for value, size in zip(values, widths)).rstrip()
            lines.append(label + ' ' * (label_width - cell_width(label) + 2) + body)
    lines.append(verification_summary(snapshot, width=width, now=now))
    # Removing optional decoration may satisfy a short budget; never drop a metric
    # group or a safety qualification to create room for a heading.
    if max_lines is not None and len(lines) > max_lines and not notices:
        lines.pop(0)
    return '\n'.join(fit_text(line, width) for line in lines)
