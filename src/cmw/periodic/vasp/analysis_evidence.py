"""Small native-evidence operations shared by historical and live analysis.

Callers own source generations, evaluation selection and timer association.
These functions neither read files nor limit the number of historical records.
"""
from __future__ import annotations

import re

from .results import NUMBER, _same_printed, number


LOOP_TIMING = re.compile(r'^\s*(LOOP\+?)\s*:\s*cpu\s+time\s+(' + NUMBER +
                         r')\s*:\s*real\s+time\s+(' + NUMBER + r')\s*$', re.I)


def parse_loop_timing(text: str) -> dict:
    """Parse native seconds, retaining CPU/wall separation and token precision."""
    match = LOOP_TIMING.fullmatch(text)
    if match is None:
        raise ValueError('Unsupported or malformed native LOOP timing grammar')
    cpu, wall = (number(value) for value in match.groups()[1:])
    if any(value['value'] is None or value['value'] < 0 for value in (cpu, wall)):
        raise ValueError('Native LOOP duration is negative or nonfinite')
    return {'kind': match[1].upper(), 'cpu': cpu, 'wall': wall}


def correspondence_scan(evaluation: dict, item: dict | None, previous=()) -> tuple:
    """Compare complete native/table prefixes without treating buffering as conflict."""
    if item is None:
        return 'pending', [], 'Awaiting electronic counterpart for the native evaluation'
    if item.get('version') not in (None, '6.6.1'):
        return 'unsupported', [], 'Declared electronic banner is outside VASP 6.6.1 support'
    native_rows, rows = evaluation['rows'], item['rows']
    start = max(0, len(previous) - 1)
    matched = list(previous[:start])
    for index, (native, row) in enumerate(zip(native_rows[start:], rows[start:]), start + 1):
        if row.get('malformed'):
            return 'unsupported', matched, 'Malformed complete electronic row; comparison unsupported'
        if native['label'] != index or row['label'] != index:
            return 'conflict', matched, 'Complete electronic rows disagree in ordered labels'
        if row['algorithm'] not in {'DAV', 'RMM'}:
            return 'unsupported', [], 'Electronic algorithm is outside DAV/RMM support'
        if native['algorithm'] is not None and native['algorithm'] != row['algorithm']:
            return 'conflict', matched, 'Electronic algorithms conflict across files'
        if native['energy'] is None:
            break
        if not _same_printed(native['energy'], row.get('energy')):
            return 'conflict', matched, 'Complete electronic energies conflict across files'
        matched.append(row)
    summary = item['summary']
    if summary is not None:
        if summary['native_ionic_label'] != evaluation['native']:
            return 'conflict', matched, 'Native evaluation labels conflict across files'
        for key in ('free_energy', 'sigma_to_zero'):
            actual = evaluation['energies'].get(key)
            if actual is not None and not _same_printed(actual, summary[key]):
                return 'conflict', matched, 'Complete evaluated ionic energies conflict across files'
        if evaluation['complete'] and len(native_rows) != len(rows):
            return 'conflict', matched, 'Completed evaluation electronic row counts conflict'
    if summary is None and evaluation['complete']:
        return 'pending', matched, 'Awaiting evaluated-energy counterpart; matched SCF rows remain usable'
    if len(matched) < max(len(native_rows), len(rows)) or (summary is not None and not evaluation['complete']):
        return 'pending', matched, 'Cross-file evidence is pending; complete matching rows remain usable'
    return 'matched', matched, ''


def duplicate_scan(item: dict, other: dict | None, previous=(), previous_fields=()) -> tuple:
    """Compare only shared complete columns in two electronic table streams."""
    if other is None:
        return 'PENDING', [], (), 'Duplicate electronic counterpart has not yet written this evaluation'
    if any(value.get('version') not in (None, '6.6.1') for value in (item, other)):
        return 'UNSUPPORTED', [], (), 'Electronic banner is outside VASP 6.6.1 support'
    checked, fields = list(previous), set(previous_fields)
    start = len(checked)
    for index, (left, right) in enumerate(zip(item['rows'][start:], other['rows'][start:]), start + 1):
        if left.get('malformed') or right.get('malformed'):
            return 'UNSUPPORTED', checked, tuple(sorted(fields)), 'Malformed complete electronic row; comparison unsupported'
        if left['label'] != index or right['label'] != index or left['algorithm'] != right['algorithm']:
            return 'CONFLICT', checked, tuple(sorted(fields)), 'Duplicate electronic streams disagree in ordered labels or algorithm'
        if left['algorithm'] not in {'DAV', 'RMM'}:
            return 'UNSUPPORTED', checked, tuple(sorted(fields)), 'Electronic algorithm is outside DAV/RMM support'
        common = tuple(key for key in ('energy', 'dE', 'd_eps', 'rms', 'rms_c') if key in left and key in right)
        for key in common:
            if not _same_printed(left[key], right[key]):
                return 'CONFLICT', checked, tuple(sorted(fields | {key})), 'Duplicate electronic streams disagree in ' + key
        compared = {'label', 'algorithm', *common}
        fields = fields & compared if checked else compared
        checked.append(left)
    status = 'MATCHED' if checked and len(checked) == max(len(item['rows']), len(other['rows'])) else 'PARTIAL' if checked else 'PENDING'
    reason = '' if status == 'MATCHED' else 'Newer electronic rows are source-only; duplicate counterpart not yet written'
    if checked and 'rms_c' not in fields:
        reason += ('; ' if reason else '') + 'rms(c) absent in part of overlap; excluded from checked fields'
    return status, checked, tuple(sorted(fields)), reason
