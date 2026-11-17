"""Bounded SCF classification of one bound VASP invocation (#41 Phase 2a).

Diagnostic only: never a completion gate, signal policy or scientific
acceptance, and ``Done`` stays operational completion. The caller binds OUTCAR
and stdout to one attempt and one final invocation; unbound evidence is
``unknown``. Rules and precedence follow docs/design/job-manifests.md
(Scientific result classification), reusing the result parser's grammar.
"""
from __future__ import annotations

import re
from pathlib import Path

from .result_sources import SnapshotReader, SourceLine
from .results import (BANNER, FATAL_MARKERS, ITERATION, NUMBER, below_printed, number,
                      outcar_setting_records, parse_electronic_row)

RESULTS = ('converged', 'labort', 'nelm_reached', 'crashed', 'unknown')
SCF_LABELS = {'DAV', 'RMM', 'DMP', 'CG', 'SDA', 'CGA', 'DIA'}
POSTPROCESSING_LABELS = {'EIG', 'NONE'}
CONVERGED = 'aborting loop because EDIFF is reached'
NOT_REACHED = 'EDIFF was not reached'
LABORT_OUTCAR = 'aborting loop because hard stop was set'
LABORT_STDOUT = re.compile(r'^\s*hard stop encountered!\s+aborting job\b')
TOTEN = re.compile(r'^\s*free\s+energy\s+TOTEN\s*=\s*(' + NUMBER + r')\s*eV\s*$')
MAGNETISATION = re.compile(r'^\s*number of electron\s+(' + NUMBER + r')\s+magnetization\s+(' + NUMBER + r')\s*$')
NONE_ROW = re.compile(r'^(\s*NONE)(\s+\d+\s)')
MAX_BYTES = 256 * 1024 * 1024


def _lines(lines):
    return [SourceLine(index, 0, 0, text if text.endswith('\n') else text + '\n')
            for index, text in enumerate(lines, 1)]


def _row(line):
    # NONE rows may omit the colon; the shared row grammar requires one.
    text = NONE_ROW.sub(r'\1:\2', line.text, count=1)
    row = parse_electronic_row(SourceLine(line.line_number, line.byte_start, line.byte_end, text), 'stdout')
    if row is None or row.get('malformed') or row['algorithm'] not in SCF_LABELS | POSTPROCESSING_LABELS:
        return row and {**row, 'malformed': True}
    return row


def _outcar(lines):
    state = {'banners': 0, 'settings': {}, 'iterations': [], 'energies': {}, 'fatal': [], 'labort': [],
             'converged': [], 'not_reached': [], 'magnetisation': [], 'echo': False, 'parameters': False}
    for line in lines:
        text = line.text.rstrip('\n')
        if BANNER.match(text):
            state['banners'] += 1
        if re.fullmatch(r'\s*INCAR:\s*', text):
            state['echo'] = True
        elif re.match(r'^\s*POTCAR:', text):
            state['echo'] = False
        if re.fullmatch(r'\s*Startparameter for this run:\s*', text):
            state['parameters'], state['echo'] = True, False
        if state['echo']:
            continue  # Input echoes are never engine actions.
        if not state['iterations']:
            for key, value in outcar_setting_records(line.text, parameters=state['parameters']):
                if key in {'NELM', 'EDIFF'}:
                    state['settings'][key] = value
        iteration = ITERATION.search(text)
        if iteration:
            state['iterations'].append((int(iteration[1]), int(iteration[2]), line.line_number))
        energy = TOTEN.match(text)
        if energy and state['iterations']:
            state['energies'][state['iterations'][-1][:2]] = number(energy[1])
        if FATAL_MARKERS.search(text):
            state['fatal'].append(line.line_number)
        if LABORT_OUTCAR in text:
            state['labort'].append(line.line_number)
        if CONVERGED in text:
            state['converged'].append(line.line_number)
        if NOT_REACHED in text:
            state['not_reached'].append(line.line_number)
        magnetisation = MAGNETISATION.match(text)
        if magnetisation and 'augmentation' not in text:
            state['magnetisation'].append((line.line_number, number(magnetisation[2])))
    return state


def _stdout(lines):
    rows, fatal, labort, malformed = [], [], [], False
    for line in lines:
        row = _row(line)
        if row is not None:
            malformed = malformed or bool(row.get('malformed'))
            if not row.get('malformed'):
                rows.append(row)
        if FATAL_MARKERS.search(line.text):
            fatal.append(line.line_number)
        if LABORT_STDOUT.match(line.text):
            labort.append(line.line_number)
    return rows, fatal, labort, malformed


def _final_rows(rows):
    """Rows of the last electronic evaluation (labels restart at 1 per ionic step)."""
    start = max((index for index, row in enumerate(rows) if row['label'] == 1), default=0)
    return rows[start:]


def classify(outcar_lines, stdout_lines=(), *, invocation_bound):
    """Classify one invocation from its OUTCAR and stdout lines."""
    result = {'scf_result': 'unknown', 'last_step': None, 'ionic_step': None, 'energy_ev': None,
              'delta_energy_ev': None, 'total_magnetisation_mu_b': None, 'evidence': []}
    evidence = result['evidence']
    if not invocation_bound:
        evidence.append({'source': 'queue', 'marker': 'evidence is not bound to one attempt and invocation',
                         'line': None})
        return result
    outcar = _outcar(_lines(outcar_lines))
    rows, stdout_fatal, stdout_labort, malformed = _stdout(_lines(stdout_lines))
    if outcar['banners'] != 1:
        evidence.append({'source': 'OUTCAR', 'marker': f'{outcar["banners"]} invocation banners; select one invocation',
                         'line': None})
        return result
    iterations = outcar['iterations']
    last = iterations[-1] if iterations else None
    final = [item for item in iterations if last and item[0] == last[0]]

    # Electronic rows must bind, label by label and energy by energy, to the OUTCAR evaluation.
    rows = _final_rows(rows)
    bound_rows = bool(rows) and not malformed and last is not None and len(rows) == len(final) and all(
        row['label'] == item[1] and _same(row.get('energy'), outcar['energies'].get(item[:2]))
        for row, item in zip(rows, final))
    if rows and not bound_rows:
        evidence.append({'source': 'stdout', 'marker': 'electronic rows do not match the OUTCAR evaluation',
                         'line': None})
    stdout_trusted = not rows or bound_rows
    if bound_rows:
        row = rows[-1]
        result.update(last_step=row['label'], ionic_step=last[0], energy_ev=row['energy']['value'],
                      delta_energy_ev=row['dE']['value'])
        moments = [value for line, value in outcar['magnetisation'] if line > final[0][2]]
        if moments and moments[-1]['value'] is not None:
            result['total_magnetisation_mu_b'] = moments[-1]['value']

    def found(source, marker, line):
        evidence.append({'source': source, 'marker': marker, 'line': line})

    fatal = [('OUTCAR', line) for line in outcar['fatal']] + (
        [('stdout', line) for line in stdout_fatal] if stdout_trusted else [])
    labort = [('OUTCAR', line) for line in outcar['labort']] + (
        [('stdout', line) for line in stdout_labort] if stdout_trusted else [])
    for source, line in fatal:
        found(source, 'native fatal marker', line)
    for source, line in labort:
        found(source, 'VASP LABORT hard stop', line)
    if fatal:
        result['scf_result'] = 'crashed'
        return result
    if labort:
        result['scf_result'] = 'labort'
        return result
    if last is None:
        return result
    # Only markers after the final electronic iteration belong to the final evaluation.
    converged = [line for line in outcar['converged'] if line > last[2]]
    not_reached = [line for line in outcar['not_reached'] if line > last[2]]
    if converged and not_reached:
        found('OUTCAR', 'opposing EDIFF markers in the final evaluation', converged[-1])
        return result
    postprocessing = bound_rows and rows[-1]['algorithm'] in POSTPROCESSING_LABELS
    if postprocessing:
        found('stdout', f'{rows[-1]["algorithm"]} rows do not establish SCF convergence', None)
        return result
    ediff, nelm = outcar['settings'].get('EDIFF'), outcar['settings'].get('NELM')
    if converged:
        if bound_rows and type(ediff) in (int, float) and below_printed(rows[-1]['dE'], ediff) == 'FAIL':
            found('stdout', 'final dE opposes the EDIFF convergence marker', None)
            return result
        found('OUTCAR', CONVERGED, converged[-1])
        result['scf_result'] = 'converged'
        return result
    if not_reached:
        contiguous = [item[1] for item in final] == list(range(1, len(final) + 1))
        if (type(ediff) in (int, float) and ediff > 0 and type(nelm) is int and nelm > 0
                and contiguous and final[-1][1] == nelm):
            found('OUTCAR', NOT_REACHED, not_reached[-1])
            result['scf_result'] = 'nelm_reached'
        else:
            found('OUTCAR', 'EDIFF not reached without a complete positive-EDIFF NELM history', not_reached[-1])
    return result


def _same(row_energy, outcar_energy):
    if not row_energy or not outcar_energy or row_energy['value'] is None or outcar_energy['value'] is None:
        return False
    tolerance = max(row_energy['half_last_place'], outcar_energy['half_last_place'])
    return abs(row_energy['value'] - outcar_energy['value']) <= tolerance


def _read(path, role):
    with SnapshotReader(path, role, max_bytes=MAX_BYTES) as reader:
        return [line.text for line in reader]


def classify_run(outcar, stdout=None, *, invocation_bound):
    """File entry point over bounded snapshot readers; missing stdout is empty."""
    return classify(_read(Path(outcar), 'OUTCAR'), _read(Path(stdout), 'stdout') if stdout else (),
                    invocation_bound=invocation_bound)
