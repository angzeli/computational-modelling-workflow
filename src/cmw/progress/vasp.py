"""Bounded incremental VASP 6.6.1 evidence, never scientific acceptance.

Numerical token, printed-resolution, mode and force definitions are shared with
result inspection. This adapter owns only live block/correspondence state; it has
no Jobs store, process, launcher or finalization interface.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
import math
import re
from types import SimpleNamespace

from cmw.periodic.vasp.inputs import parse_poscar
from cmw.periodic.vasp.results import (
    BANNER, ITERATION, NUMBER, SET_RE, _force_summary, _same_printed,
    _scalar, _scope, below_printed, number, parse_electronic_row,
    parse_ionic_summary,
)
from .models import (Criterion, IterationLevel, Metric, MetricSeries,
                     ProgressEvent, ProgressSnapshot, HistoryEntry, HistoryColumn, Timing, Verification, SettingObservation, comparable)

MAX_EVALUATIONS = 16
MAX_ITERATIONS = 1024
MAX_ATOMS = 10000
MAX_POSCAR_BYTES = 1024 * 1024
MAX_HISTORY_LINES = 2048
MAX_PENDING_HISTORY = 2 * MAX_EVALUATIONS * (MAX_ITERATIONS + 1) + 2
LOOP_TIMING = re.compile(r'^\s*(LOOP\+?)\s*:\s*cpu\s+time\s+(' + NUMBER +
                         r')\s*:\s*real\s+time\s+(' + NUMBER + r')\s*$', re.I)


def _batch_line(line):
    ref = line.evidence
    return SimpleNamespace(text=line.text, line_number=ref.line_start,
                           byte_start=ref.byte_start, byte_end=ref.byte_end)


def _span(first, last):
    return replace(first, line_end=last.line_end, byte_end=last.byte_end)


def _relation(parsed, criterion):
    if criterion is None:
        return None
    relation = {'PASS': 'BELOW', 'FAIL': 'AT_OR_ABOVE', 'UNKNOWN': 'UNCERTAIN',
                'NOT_APPLICABLE': 'NOT_APPLICABLE'}[below_printed(parsed, criterion.value)]
    return 'PROVISIONAL_' + relation if criterion.kind == 'provisional-engine-stopping' else relation


@dataclass
class _ElectronicStream:
    """One declared file's bounded native sequence; never shared parser state."""

    evaluations: deque = field(default_factory=deque)
    segment: int = 1
    banner_seen: bool = False
    next_index: int = 1
    current: dict | None = None
    last_native: int | None = None
    serial: int = 0


class VaspAdapter:
    """Consume bounded complete lines for explicit OUTCAR/electronic sources.

    At most 16 recent evaluations and 1024 rows per evaluation are retained.
    Overflow is explicit unresolved evidence, never a guessed correspondence.
    Independent source resets cannot rebind a stale sibling stream.
    """

    def __init__(self, context):
        self.context = context
        self.states = {}
        self.generations = {}
        self.asymmetric_reset = False
        self.out_segment = 0
        self.out = None
        self.flags = None
        self.flags_key = None
        self.poscar_text = ''
        self.poscar_revision = 0
        self.poscar_cache_key = None
        self.poscar_evidence = None
        self.poscar_issue = None
        self.events = deque(maxlen=64)
        self.last_new = None
        self.issues = deque(maxlen=32)
        self.streams = {role: _ElectronicStream() for role in ('oszicar', 'stdout')
                        if any(source.role.lower() == role for source in context.sources)}
        self.electronic_role = next(iter(self.streams), None)
        self.selection_observed = False
        self.stream_conflicts = deque(maxlen=32)
        self.incar = {}
        self.incar_issues = []
        self.pending_lines = {}
        self.history_updates = {}
        self.history_sequence = 0
        self.history_dirty = set()
        self.pending_electronic_history = {}
        self.local_history = {}
        self.segment_dirty = False
        self.out_serial = 0
        self.observation = (None, True, None)

    def set_observation(self, monotonic_now, *, historical, clock_id):
        self.observation = (monotonic_now, historical, clock_id)

    def read_roles(self):
        if len(self.history_updates) >= MAX_PENDING_HISTORY:
            return ()
        return tuple(source.role for source in self.context.sources
                     if not self.pending_lines.get(source.role.lower()))

    @property
    def has_pending_work(self):
        return any(self.pending_lines.values())

    def drain_history(self):
        updates = tuple(sorted(self.history_updates.values(), key=lambda item: item.sequence))
        self.history_updates.clear()
        return updates

    def _history_identity(self, record, line, kind):
        self.history_sequence += 1
        ref = line.evidence
        record['history_id'] = ':'.join(map(str, (self.context.binding_id,
            self.context.attempt_id, self.context.stage, ref.source_role,
            ref.path, ref.generation, ref.file_identity, ref.byte_start, kind)))
        record['history_sequence'] = self.history_sequence
        record['observer_start'] = None
        record['observer_end'] = None
        record['observer_clock'] = None

    @property
    def osz(self):
        stream = self.streams.get(self.electronic_role)
        return stream.evaluations if stream else ()

    @property
    def osz_current(self):
        stream = self.streams.get(self.electronic_role)
        return stream.current if stream else None

    @property
    def osz_segment(self):
        stream = self.streams.get(self.electronic_role)
        return stream.segment if stream else 1

    def _select_electronic(self):
        candidates = []
        for role, stream in self.streams.items():
            usable = [item for item in stream.evaluations if self._usable_rows(item)]
            if usable:
                item = usable[-1]
                candidates.append(((item['segment'], item['index'], len(self._usable_rows(item))), role))
        if not candidates:
            return
        frontier = max(value for value, _ in candidates)
        tied = [role for value, role in candidates if value == frontier]
        selected = self.electronic_role if self.selection_observed and self.electronic_role in tied else tied[0]
        if selected != self.electronic_role or not self.selection_observed:
            self.electronic_role = selected
            item = next(item for item in reversed(self.streams[selected].evaluations) if self._usable_rows(item))
            row = self._usable_rows(item)[-1]
            self._event('display-source', row['line'], 'Electronic display source selected: ' + selected)
        self.selection_observed = True

    @staticmethod
    def _usable_rows(item):
        # Keep complete source-local values even if an unavailable counterpart lags.
        # A malformed or discontinuous row never becomes a fabricated prefix.
        result = []
        for row in item['rows']:
            if (row.get('malformed') or row['label'] != len(result) + 1
                    or row['algorithm'] not in {'DAV', 'RMM'}
                    or any(row.get(key, {}).get('value') is None for key in ('energy', 'dE', 'd_eps', 'rms'))):
                break
            result.append(row)
        return result

    def _event(self, kind, line, message, context=()):
        ref = line.evidence
        identity = ':'.join(map(str, (self.context.attempt_id or 'explicit',
                                     self.context.binding_id, ref.generation, ref.file_identity,
                                     self.context.stage, self.out_segment,
                                     tuple((v.kind, v.index) for v in context),
                                     ref.source_role, ref.byte_start, kind)))
        self.events.append(ProgressEvent(identity, kind, self.context.stage,
                                        max(1, self.out_segment), context, (ref,), message))
        self.last_new = line.observed_at

    def _issue(self, message):
        if message not in self.issues:
            self.issues.append(message)

    def _new_out(self, line, version=None):
        if self.out and self.out['current']:
            self._close_evaluation(self.out['current'], line)
        self._publish_history()
        self.pending_electronic_history.clear()
        self.out_segment += 1
        self.out = {'version': version, 'settings': {}, 'echoed_incar': {},
                    'in_echo': False, 'nions': None, 'evaluations': deque(),
                    'next': 1, 'current': None, 'force': None,
                    'forces': deque(maxlen=2), 'conflicts': [], 'in_energy': False,
                    'fatal': False, 'interrupted': False, 'footer': False,
                    'timing': set(), 'footer_timings': {}, 'line': line, 'ionic_stop': False}
        self._history_identity(self.out, line, 'segment')
        self.segment_dirty = True
        self._event('segment', line, 'Native run segment observed')

    def _out_conflict(self, message):
        if message not in self.out['conflicts']:
            self.out['conflicts'].append(message)
            del self.out['conflicts'][:-32]

    def _levels(self, evaluation, electronic=None):
        values = self._values()
        levels = (IterationLevel('evaluation', evaluation['index'],
                                 f"evaluation {evaluation['index']} (native {evaluation['native']})"),)
        if electronic is not None:
            budget = values.get('NELM')
            budget = budget if type(budget) is int and budget > 0 else None
            levels += (IterationLevel('electronic', electronic, f'SCF {electronic}', budget),)
        return levels

    def _values(self):
        return {key: val['value'] for key, val in self.out['settings'].items()} if self.out else {}

    def _read_out(self, line):
        text = line.text
        banner = BANNER.match(text)
        if banner or self.out is None:
            self._new_out(line, banner[1] if banner else None)
        out = self.out
        evaluation = out['current']
        if evaluation is not None:
            evaluation['revision'] += 1
            self.history_dirty.add(evaluation['serial'])
        if 'INCAR:' in text:
            out['in_echo'] = True
        if 'POTCAR:' in text:
            out['in_echo'] = False
        if evaluation is None:
            for match in SET_RE.finditer(text):
                target = out['echoed_incar'] if out['in_echo'] else out['settings']
                key, value = match[1].upper(), _scalar(match[2])
                if key in target and target[key]['value'] != value:
                    self._out_conflict('conflicting effective/echoed setting: ' + key)
                target[key] = {'value': value, 'reference': line.evidence}
        if evaluation is not None:
            for match in SET_RE.finditer(text):
                previous = out['settings'].get(match[1].upper())
                if previous is not None and previous['value'] != _scalar(match[2]):
                    self._out_conflict('effective setting changed after SCF began: ' + match[1])
        count = re.search(r'\bNIONS\s*=\s*(\d+)', text)
        if count:
            if out['nions'] is not None and out['nions'] != int(count[1]):
                self._out_conflict('NIONS changed within a native segment')
            out['nions'] = int(count[1])
            if not 0 < out['nions'] <= MAX_ATOMS:
                self._out_conflict('atom count exceeds bounded force support')
        iteration = ITERATION.search(text)
        if iteration:
            ionic, electronic = map(int, iteration.groups())
            if out['footer'] or out['ionic_stop']:
                self._out_conflict('electronic evaluation after native termination evidence')
            if evaluation is None or electronic == 1 or ionic != evaluation['native']:
                if out['force'] is not None:
                    self._out_conflict('force block interrupted by a new evaluation')
                if evaluation is not None:
                    self._close_evaluation(evaluation, line)
                evaluation = {'index': out['next'], 'native': ionic, 'rows': [],
                              'energies': {}, 'energy_refs': {}, 'force': None, 'complete': False, 'closed': False,
                              'native_convergence': None, 'line': line, 'end_line': None,
                              'timings': (), 'timing_issue': None, 'trial': [], 'force_record': None, 'revision': 0}
                self.out_serial += 1
                evaluation['serial'] = self.out_serial
                self.history_dirty.add(self.out_serial)
                self._history_identity(evaluation, line, 'evaluation')
                out['next'] += 1
                if len(out['evaluations']) == MAX_EVALUATIONS:
                    dropped = out['evaluations'][0]
                    if dropped['serial'] in self.history_dirty:
                        self._publish_evaluation(dropped)
                    out['evaluations'].popleft()
                    self.pending_electronic_history = {key: value for key, value in self.pending_electronic_history.items() if key[0] != dropped['history_id']}
                    if not any(item['segment'] == self.out_segment and item['index'] == dropped['index'] for item in self.osz):
                        self._issue('Native evaluation buffer evicted unmatched evidence; earlier correspondence unavailable')
                out['evaluations'].append(evaluation)
                out['current'] = evaluation
                if electronic != 1:
                    self._out_conflict('missing electronic prefix')
            elif evaluation['rows'] and electronic != evaluation['rows'][-1]['label'] + 1:
                self._out_conflict('electronic label gap')
            if len(evaluation['rows']) >= MAX_ITERATIONS:
                self._out_conflict('electronic iteration buffer limit reached')
            else:
                row = {'label': electronic, 'energy': None, 'algorithm': None,
                       'line': line, 'timings': (), 'timing_issue': None}
                previous_sequence = self.history_sequence
                self._history_identity(row, line, 'electronic')
                key = (evaluation['history_id'], electronic)
                pending = self.pending_electronic_history.pop(key, None)
                row['history_id'] = evaluation['history_id'] + ':electronic:' + str(electronic)
                local = self.local_history.get((self.out_segment, evaluation['index']))
                if local and evaluation.get('local_history_adopted'):
                    pending = pending or local['rows'].get(electronic)
                if pending is not None:
                    row['history_sequence'] = pending['history_sequence']
                    self.history_sequence = previous_sequence
                evaluation['rows'].append(row)
            out['force'] = None
            out['in_energy'] = False
            self._event('iteration', line, 'Native electronic iteration observed',
                        self._levels(evaluation, electronic))
        if re.match(r'^\s*LOOP\+?\s*:', text, re.I):
            self._read_loop_timing(line)
        if evaluation and re.search(r'\b(?:trial:|trial-energy|ZBRENT:|curvature:)', text):
            evaluation['trial'].append(line.evidence)
            del evaluation['trial'][:-8]
        if evaluation and evaluation['rows']:
            row = evaluation['rows'][-1]
            if 'EDDAV:' in text:
                row['algorithm'] = 'DAV'
            if re.search(r'\b(?:RMM-DIIS|RMM:)', text):
                row['algorithm'] = 'RMM'
            native = (True if 'aborting loop because EDIFF is reached' in text else
                      False if 'EDIFF was not reached' in text else None)
            if native is not None:
                if evaluation['native_convergence'] is not None and evaluation['native_convergence'] != native:
                    self._out_conflict('opposing native electronic convergence evidence')
                evaluation['native_convergence'] = native
                self._event('native-convergence', line,
                            'Native EDIFF reached' if native else 'Native EDIFF not reached',
                            self._levels(evaluation, row['label']))
        if 'POSITION' in text and 'TOTAL-FORCE' in text:
            if evaluation is None:
                self._out_conflict('force block lacks a native evaluation')
            elif evaluation['force'] is not None:
                self._out_conflict('multiple force blocks in one evaluation')
            else:
                out['force'] = {'rows': [], 'line': line, 'complete_rows': False,
                                'evaluation': evaluation}
            return
        block = out['force']
        if block is not None:
            tokens = text.split()
            separator = bool(text.strip()) and set(text.strip()) == {'-'}
            if block['complete_rows']:
                if not text.strip():
                    return
                if separator:
                    self._finish_force(block, line)
                else:
                    self._out_conflict('force block terminator is not supported')
                out['force'] = None
                return
            if not text.strip() or separator:
                return
            if len(tokens) >= 6 and all(re.fullmatch(NUMBER, t, re.I) for t in tokens[:6]):
                parsed = [number(t) for t in tokens[:6]]
                if any(item['value'] is None for item in parsed):
                    self._out_conflict('nonfinite force/geometry row')
                    out['force'] = None
                    return
                block['rows'].append(parsed)
                count = out['nions']
                if count is None or count < 1 or count > MAX_ATOMS:
                    self._out_conflict('force block lacks a supported NIONS count')
                    out['force'] = None
                elif len(block['rows']) == count:
                    block['complete_rows'] = True
                return
            self._out_conflict('incomplete or malformed force block')
            out['force'] = None
        if 'FREE ENERGIE OF THE ION-ELECTRON SYSTEM' in text:
            out['in_energy'] = True
        toten = re.search(r'free\s+energy\s+TOTEN\s*=\s*(' + NUMBER + ')', text, re.I)
        if toten and evaluation and evaluation['rows']:
            parsed = number(toten[1])
            if out['in_energy']:
                evaluation['energies']['free_energy'] = parsed
                evaluation['energy_refs']['free_energy'] = line.evidence
            else:
                evaluation['rows'][-1]['energy'] = parsed
                evaluation['rows'][-1]['energy_ref'] = line.evidence
            self.last_new = line.observed_at
        energies = re.search(r'energy\s+without entropy\s*=\s*(' + NUMBER + r').*energy\(sigma->0\)\s*=\s*(' + NUMBER + ')', text, re.I)
        if energies and evaluation and out['in_energy']:
            evaluation['energy_refs'].update({key: line.evidence for key in ('without_entropy', 'sigma_to_zero')})
            evaluation['energies'].update(zip(('without_entropy', 'sigma_to_zero'),
                                              (number(t) for t in energies.groups())))
            evaluation['complete'] = (evaluation['force'] is not None and
                                      evaluation['energies'].get('free_energy', {}).get('value') is not None and
                                      all(v['value'] is not None for v in evaluation['energies'].values()))
            evaluation['end_line'] = line
            out['in_energy'] = False
            self._event('evaluated-energy', line, 'Evaluated ionic energy observed', self._levels(evaluation))
        if 'reached required accuracy - stopping structural energy minimisation' in text:
            out['ionic_stop'] = True
            self._event('native-optimizer-stop', line, 'Native optimizer stopping evidence')
        if 'General timing and accounting informations for this job:' in text:
            out['footer'] = True
            self._event('native-footer-start', line, 'Native timing footer began; not a scientific verdict')
        timing = re.search(r'(Total CPU time used|User time|System time|Elapsed time)\s*\(sec\):\s*(' + NUMBER + ')', text)
        if timing and out['footer'] and number(timing[2])['value'] is not None:
            out['timing'].add(timing[1])
            out['footer_timings'][timing[1]] = (number(timing[2]), line)
            self.segment_dirty = True
            if len(out['timing']) == 4:
                self._event('native-footer', line, 'Complete native timing footer observed')
        if re.search(r'VERY BAD NEWS|internal error|ERROR FEXCP|ZHEGV failed|ERROR EDD|killed by signal', text, re.I):
            out['fatal'] = True
            self._event('native-fatal', line, 'Native fatal/interruption text observed')
        if re.search(r'LSTOP\s*=\s*T|LABORT\s*=\s*T|STOPCAR.*(?:stop|found)|soft stop', text, re.I):
            out['interrupted'] = True
            self._event('native-stop-request', line, 'Native stop-request text observed')

    def _close_evaluation(self, evaluation, line):
        evaluation['closed'] = True
        if evaluation['end_line'] is None:
            evaluation['end_line'] = line
        self.history_dirty.add(evaluation['serial'])
        # Freeze an observed interval at the observed structural boundary, even
        # when native force/energy evidence for the old evaluation is missing.
        mono, historical, _ = self.observation
        if (not historical and evaluation['observer_start'] is not None
                and evaluation['observer_end'] is None):
            evaluation['observer_end'] = mono

    def _read_loop_timing(self, line):
        """Associate modern native timers only inside their structural interval."""
        match = LOOP_TIMING.fullmatch(line.text)
        outer = bool(re.match(r'^\s*LOOP\+', line.text, re.I))
        out, evaluation = self.out, self.out['current']
        if evaluation is None or not evaluation['rows'] or out['footer']:
            self._issue('Unassociated LOOP timing: no open native evaluation')
            return
        record = evaluation if outer else evaluation['rows'][-1]
        if outer and not evaluation['complete']:
            record['timing_issue'] = 'LOOP+ lacks a completed native force/energy evaluation'
            return
        if not outer and (evaluation['force'] is not None or out['in_energy']):
            record['timing_issue'] = 'LOOP occurs outside its electronic interval'
            return
        if match is None:
            record['timing_issue'] = 'Unsupported or malformed native LOOP timing grammar'
            return
        parsed = tuple(number(value) for value in match.groups()[1:])
        if any(value['value'] is None or value['value'] < 0 for value in parsed):
            record['timing_issue'] = 'Native LOOP duration is negative or nonfinite'
            return
        if record['timings']:
            record['timings'] = ()
            record['timing_issue'] = 'Multiple timing records in one native interval are ambiguous'
            return
        if record['timing_issue']:
            return
        record['timings'] = tuple((kind, value, line) for kind, value in zip(('cpu', 'wall'), parsed))
        self.last_new = line.observed_at

    def _finish_force(self, block, line):
        vectors = [[value['value'] for value in row[3:]] for row in block['rows']]
        flags = self.flags if self.flags is not None and len(self.flags) == len(vectors) else None
        summary = _force_summary(vectors, flags)
        if any(group is not None and group['maximum_norm'] is not None and not math.isfinite(group['maximum_norm'])
               for group in (summary['all'], summary['free'], summary['fixed'])):
            self._out_conflict('derived force norm is nonfinite')
            return
        if flags is None:
            summary['free'] = summary['fixed'] = None
        record = {'summary': summary, 'flags_key': (self.poscar_revision, self.flags_key) if flags is not None else None,
                  'constraint_evidence': self.poscar_evidence if flags is not None else None,
                  'precision': max(v['half_last_place'] for row in block['rows'] for v in row[3:]),
                  'evidence': _span(block['line'].evidence, line.evidence),
                  'at': line.observed_at, 'evaluation': {key: block['evaluation'][key] for key in ('index', 'native')}}
        block['evaluation']['force'] = True
        block['evaluation']['force_record'] = record
        self.out['forces'].append(record)
        self._event('force', line, 'Complete atomic-vector force block observed',
                    self._levels(block['evaluation']))

    def _new_osz_evaluation(self, stream):
        stream.serial += 1
        item = {'index': stream.next_index, 'segment': stream.segment, 'rows': [],
                'summary': None, 'serial': stream.serial}
        stream.next_index += 1
        if len(stream.evaluations) >= MAX_EVALUATIONS:
            dropped = stream.evaluations[0]
            if self.out is not None:
                native = next((entry for entry in self.out['evaluations'] if entry['serial'] == dropped['serial']), None)
                if native is not None:
                    self._publish_evaluation(native)
            stream.evaluations.popleft()
            self.history_dirty.discard(dropped['serial'])
            if self.out is not None and self.out['current'] and dropped['segment'] >= self.out_segment and dropped['index'] >= self.out['current']['index']:
                self._issue('Unmatched electronic evaluation buffer limit reached')
        stream.evaluations.append(item)
        stream.current = item
        return item

    def _read_electronic(self, line, stream):
        if BANNER.match(line.text):
            if stream.banner_seen or stream.evaluations:
                stream.segment += 1
            stream.banner_seen = True
            stream.next_index = 1
            stream.current = None
            stream.last_native = None
            return
        row = parse_electronic_row(_batch_line(line), line.evidence.source_role)
        if row is not None:
            if stream.current is None or (row['label'] == 1 and stream.current['rows']):
                self._new_osz_evaluation(stream)
            item = stream.current
            if len(item['rows']) >= MAX_ITERATIONS:
                self._issue('Electronic source row buffer limit reached')
                return
            row['line'] = line
            item['rows'].append(row)
            self.history_dirty.add(item['serial'])
            self.last_new = line.observed_at
        summary = parse_ionic_summary(_batch_line(line), line.evidence.source_role)
        if summary is not None:
            item = stream.current or self._new_osz_evaluation(stream)
            # A reset summary is an explicit segment boundary, never a match by energy alone.
            if (stream.last_native is not None
                    and summary['native_ionic_label'] <= stream.last_native
                    and stream.segment < self.out_segment):
                stream.segment += 1
                item['segment'] = stream.segment
                item['index'] = 1
                stream.next_index = 2
            item['summary'] = summary
            self.history_dirty.add(item['serial'])
            stream.last_native = summary['native_ionic_label']
            stream.current = None
            self.last_new = line.observed_at

    def _read_incar(self, update):
        if update.reset:
            self.incar.clear()
            self.incar_issues.clear()
        for line in update.lines:
            text = re.split(r'[!#]', line.text, maxsplit=1)[0]
            for match in SET_RE.finditer(text):
                key, value = match[1].upper(), _scalar(match[2])
                if key in self.incar and self.incar[key]['value'] != value:
                    self.incar_issues.append('Conflicting declared INCAR value: ' + key)
                self.incar[key] = {'value': value, 'reference': line.evidence}
        del self.incar_issues[:-32]

    def _settings(self):
        effective = self.out['settings'] if self.out else {}
        state = self.states.get('incar')
        provisional = self.incar if state and state.health == 'OK' and not state.has_more and not state.pending_bytes else {}
        result = []
        for key in sorted(effective.keys() | provisional.keys()):
            actual, requested = effective.get(key), provisional.get(key)
            record = actual or requested
            result.append(SettingObservation(key, record['value'],
                'OUTCAR effective ' + key if actual else 'Declared INCAR (historical linkage unestablished)',
                'effective' if actual else 'provisional', (record['reference'],),
                requested['value'] if actual and requested else None,
                bool(actual and requested and actual['value'] != requested['value'])))
        return tuple(result)

    def _read_poscar(self, update):
        if update.reset:
            self.poscar_text = ''
            self.flags = self.flags_key = None
            self.poscar_evidence = None
            self.poscar_revision += 1
        text = ''.join(line.text.rstrip('\r\n') + '\n' for line in update.lines)
        if len(self.poscar_text.encode('utf-8')) + len(text.encode('utf-8')) > MAX_POSCAR_BYTES:
            self.poscar_issue = 'Current POSCAR exceeds the bounded constraint reader'
            self.flags = self.flags_key = None
            return
        self.poscar_text += text
        if update.lines:
            self.poscar_evidence = _span(self.poscar_evidence or update.lines[0].evidence,
                                         update.lines[-1].evidence)
        state = update.state
        cache_key = (state.generation, state.offset, state.pending_bytes, state.has_more, state.health)
        if cache_key == self.poscar_cache_key and not update.reset:
            return
        self.poscar_cache_key = cache_key
        if state.has_more or state.pending_bytes or not self.poscar_text or state.health != 'OK':
            self.flags = self.flags_key = None
            self.poscar_issue = 'Current POSCAR constraints are pending or unavailable'
            return
        parsed = parse_poscar(self.poscar_text)
        if parsed.get('status') != 'valid':
            self.flags = self.flags_key = None
            self.poscar_issue = 'Current POSCAR constraints are unavailable or unsupported'
            return
        count = parsed['atom_count']
        flags = parsed.get('selective_dynamics') or [[True] * 3 for _ in range(count)]
        key = tuple(tuple(row) for row in flags)
        if key != self.flags_key:
            self.poscar_revision += 1
        self.flags, self.flags_key = flags, key
        self.poscar_issue = None

    def _correspondence(self, evaluation, role=None):
        role = role or self.electronic_role
        stream = self.streams.get(role)
        item = next((entry for entry in stream.evaluations if entry['segment'] == self.out_segment
                     and entry['index'] == evaluation['index']), None) if stream else None
        signature = (self.out_segment, self.asymmetric_reset, self.generations.get(role),
                     self.generations.get('outcar'), evaluation['revision'],
                     len(item['rows']) if item else None, repr(item['summary']) if item else None)
        cache = evaluation.setdefault('correspondence_cache', {})
        previous = cache.get(role)
        if previous is not None and previous[0] == signature:
            return previous[1]
        # Complete rows are immutable inside one reader generation. Only the last
        # checked native row can still acquire its algorithm/energy field.
        matched = previous[1][1] if previous and previous[0][:4] == signature[:4] else ()
        result = self._correspondence_scan(evaluation, role, matched)
        cache[role] = (signature, result)
        return result

    def _correspondence_scan(self, evaluation, role=None, previous=()):
        if self.asymmetric_reset:
            return 'unresolved', [], 'Source generations changed independently; sibling correspondence is unestablished'
        stream = self.streams.get(role or self.electronic_role)
        item = next((v for v in stream.evaluations if v['segment'] == self.out_segment
                     and v['index'] == evaluation['index']), None) if stream else None
        if item is None and self.osz_segment > self.out_segment:
            return 'unresolved', [], 'Electronic source segment cannot be associated with the current native segment'
        if item is None:
            return 'pending', [], 'Awaiting electronic counterpart for the native evaluation'
        native_rows, rows = evaluation['rows'], item['rows']
        if self.out['evaluations'] and evaluation['index'] < self.out['evaluations'][0]['index']:
            return 'unresolved', [], 'Native correspondence buffer was exceeded'
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

    def _remember_stream_conflict(self, message, segment, roles):
        entry = (segment, message, frozenset(roles))
        if entry not in self.stream_conflicts:
            self.stream_conflicts.append(entry)

    def _duplicate_check(self, item, role, other_role):
        other = next((candidate for candidate in self.streams[other_role].evaluations
                      if candidate['segment'] == item['segment'] and candidate['index'] == item['index']), None)
        signature = (self.asymmetric_reset, self.generations.get(role), self.generations.get(other_role),
                     item['segment'], item['index'], len(item['rows']), len(other['rows']) if other else None)
        cache = item.setdefault('duplicate_cache', {})
        previous = cache.get(other_role)
        if previous is not None and previous[0] == signature:
            return previous[1]
        checked = previous[1][1] if previous and previous[0][:5] == signature[:5] else ()
        fields = previous[1][2] if checked else ()
        result = self._duplicate_scan(item, role, other_role, checked, fields)
        cache[other_role] = (signature, result)
        return result

    def _duplicate_scan(self, item, role, other_role, previous=(), previous_fields=()):
        other = next((candidate for candidate in self.streams[other_role].evaluations
                      if candidate['segment'] == item['segment'] and candidate['index'] == item['index']), None)
        if self.asymmetric_reset:
            return 'UNRESOLVED', [], (), 'Source generations changed independently; duplicate association unestablished'
        if other is None:
            return 'PENDING', [], (), 'Duplicate electronic counterpart has not yet written this evaluation'
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

    def _check_all_streams(self):
        # Revisit retained prefixes on every source advance, including late peers.
        # Conflicts remain latched for this segment even after later rows agree.
        for role, stream in self.streams.items():
            if self.out is not None:
                for evaluation in self.out['evaluations']:
                    status, _, message = self._correspondence(evaluation, role)
                    if status == 'conflict':
                        self._remember_stream_conflict(f"Evaluation {evaluation['index']}: {message} ({role}/OUTCAR)", self.out_segment, (role, 'outcar'))
            for item in stream.evaluations:
                if len(self._usable_rows(item)) < len(item['rows']):
                    self._issue(role + ': malformed, unsupported or discontinuous electronic row; only complete supported prefix is usable')
                for other_role in self.streams:
                    if role >= other_role:
                        continue
                    status, _, _, message = self._duplicate_check(item, role, other_role)
                    if status == 'CONFLICT':
                        self._remember_stream_conflict(f"{role}/{other_role} evaluation {item['index']}: {message}", item['segment'], (role, other_role))

    def _selected_observation(self):
        stream = self.streams.get(self.electronic_role)
        if stream is None:
            return [], (), ()
        items = [item for item in stream.evaluations if self._usable_rows(item)
                 and (not self.out or item['segment'] >= self.out_segment)]
        if not items:
            state = self.states.get(self.electronic_role)
            malformed = any(item['rows'] for item in stream.evaluations)
            status = 'UNSUPPORTED' if malformed else 'UNAVAILABLE' if state and state.health not in {'OK', 'PENDING'} else 'PENDING'
            reason = ('Electronic stream lacks a supported ordered complete prefix' if malformed else
                      'Electronic source unavailable: ' + state.health if status == 'UNAVAILABLE' else
                      'Declared electronic source has not written complete supported rows')
            if self.out and stream.evaluations:
                reason = 'Electronic source segment cannot be associated with the current native segment'
            return [], (Verification('electronic-row', status, self.electronic_role, 'outcar',
                        reason=reason, source_generation=self.generations.get(self.electronic_role),
                        counterpart_generation=self.generations.get('outcar')),), ()
        item = items[-1]
        rows = self._usable_rows(item)
        native = (next((entry for entry in self.out['evaluations'] if entry['index'] == item['index']), None)
                  if self.out and item['segment'] == self.out_segment else None)
        status, matched, reason = (self._correspondence(native) if native else
            ('pending', [], 'OUTCAR has not yet written an associated native evaluation'))
        associated = bool(matched) and status not in {'unresolved', 'conflict', 'unsupported'}
        context = self._levels(native) if associated else ()
        native_frontier = native['rows'][-1]['label'] if native and native['rows'] else None
        out_state = self.states.get('outcar')
        if status == 'pending':
            state = 'PARTIAL' if matched else 'PENDING'
            if out_state is None or out_state.health not in {'OK', 'PENDING'}:
                state = 'UNAVAILABLE'
        else:
            state = status.upper()
        compared = ('label', 'energy') + (('algorithm',) if matched and all(row['algorithm'] for row in native['rows'][:len(matched)]) else ())
        refs = ((matched[-1]['line'].evidence, native['rows'][len(matched)-1]['energy_ref']) if matched else ())
        verification = [Verification('electronic-row', state, self.electronic_role, 'outcar',
            rows[-1]['label'], matched[-1]['label'] if matched else None, native_frontier,
            context, compared if matched else (),
            'Sum of printed half-last-place intervals plus 1e-12 eV (shared parser)', reason,
            self.generations.get(self.electronic_role), self.generations.get('outcar'), refs)]
        for other_role in self.streams:
            if other_role == self.electronic_role:
                continue
            duplicate, checked, fields, message = self._duplicate_check(item, self.electronic_role, other_role)
            other_state = self.states.get(other_role)
            if duplicate == 'PENDING' and other_state and other_state.health not in {'OK', 'PENDING'}:
                duplicate, message = 'UNAVAILABLE', 'Duplicate source unavailable: ' + other_state.health
            verification.append(Verification('electronic-stream', duplicate, self.electronic_role, other_role,
                rows[-1]['label'], checked[-1]['label'] if checked else None,
                context=context, fields=fields,
                precision_policy='Sum of printed half-last-place intervals plus 1e-12 (shared parser)',
                reason=message, source_generation=self.generations.get(self.electronic_role),
                counterpart_generation=self.generations.get(other_role)))
        conflicts = [message for segment, message, _ in self.stream_conflicts if segment == item['segment']]
        qualification = ('conflict' if conflicts else 'row-correspondence' if matched and matched[-1]['label'] == rows[-1]['label'] else 'source-local')
        metrics = self._electronic_metrics(native or {}, rows, qualification=qualification, associated=associated, segment=item['segment'])
        if conflicts or status in {'conflict', 'unresolved', 'unsupported'} or (self.out and item['segment'] != self.out_segment):
            metrics = [MetricSeries(replace(series.latest, qualification='conflict' if conflicts else 'source-local', criterion=None, relation=None),
                       replace(series.previous, qualification='conflict' if conflicts else 'source-local', criterion=None, relation=None) if series.previous else None)
                       for series in metrics]
        for segment, message, roles in self.stream_conflicts:
            if segment == item['segment']:
                left, right = sorted(roles)
                verification.append(Verification('retained-electronic-conflict', 'CONFLICT', left, right,
                    context=context, reason=message, source_generation=self.generations.get(left),
                    counterpart_generation=self.generations.get(right)))
        local_context = (IterationLevel('electronic', rows[-1]['label'], f"SCF {rows[-1]['label']}"),)
        return metrics, tuple(verification), local_context

    def _criterion(self, key, unit, definition):
        setting = next((item for item in self._settings() if item.name == key), None)
        value = setting.value if setting else None
        origin = setting.origin if setting else ''
        if setting and setting.qualification == 'provisional':
            definition = definition.replace('effective EDIFF', 'declared provisional EDIFF') + '; historical linkage unestablished'
        if type(value) not in (int, float) or not math.isfinite(value):
            return None
        if key == 'EDIFF' and value > 0:
            return Criterion(value, unit, '<', origin, kind='provisional-engine-stopping' if setting.qualification == 'provisional' else 'engine-stopping', definition=definition,
                             signed_value=value)
        if key == 'EDIFFG' and value < 0:
            return Criterion(-value, unit, '<', origin, kind='provisional-engine-stopping' if setting.qualification == 'provisional' else 'engine-stopping', definition=definition,
                             signed_value=value)
        return None

    def _targets(self, semantic_id):
        return tuple(v.criterion for v in self.context.diagnostic_targets if v.semantic_id == semantic_id)

    def _electronic_metrics(self, evaluation, rows, *, qualification='observed', associated=True, segment=None):
        result = []
        if not rows:
            return result
        for key, label, definition, unit, magnitude, priority in (
            ('energy', 'Electronic energy', 'Electronic iteration free energy; not evaluated ionic energy', 'eV', False, 60),
            ('dE', '|dE|', 'Magnitude of electronic total-energy change within one SCF evaluation', 'eV', True, 20),
            ('d_eps', '|d eps|', 'Magnitude of electronic band-energy change within one SCF evaluation', 'eV', True, 21),
            ('rms', 'rms', 'Native electronic wavefunction residual norm', 'native', False, 40),
            ('rms_c', 'rms(c)', 'Native charge-density residual norm', 'native', False, 30),
        ):
            semantic_id = 'vasp.electronic.' + ('free_energy' if key == 'energy' else key)
            criterion = self._criterion('EDIFF', 'eV', 'Both dE and d eps magnitudes must satisfy effective EDIFF') if magnitude else None
            def metric(row):
                value = row.get(key)
                # A missing current rms(c) stays unavailable; do not promote an older value.
                parsed = value or {'value': None, 'half_last_place': None, 'printed': None}
                raw = parsed['value']
                return Metric(semantic_id, label, definition, unit,
                              abs(raw) if magnitude and raw is not None else raw,
                              (self._levels(evaluation, row['label']) if associated else
                               (IterationLevel('electronic', row['label'], f"SCF {row['label']}"),)), self.context.stage,
                              segment or max(1, self.out_segment), parsed['half_last_place'], parsed['printed'],
                              raw if magnitude else None, qualification, (row['line'].evidence,),
                              row['line'].observed_at, criterion, self._targets(semantic_id),
                              _relation(parsed, criterion), priority,
                              comparison_context=self._levels(evaluation) if associated else ())
            latest = metric(rows[-1])
            previous = metric(rows[-2]) if len(rows) > 1 and rows[-2]['label'] + 1 == rows[-1]['label'] else None
            result.append(MetricSeries(latest, previous if previous is not None and comparable(latest, previous) else None))
        return result

    def _force_metrics(self, records=None):
        records = self.out['forces'] if records is None else records
        if not records:
            return []
        result = []
        for scope, priority in (('all', 50), ('free', 10), ('fixed', 51)):
            latest = records[-1]
            group = latest['summary'].get(scope)
            if scope == 'all' and not (latest['summary'].get('free') or {}).get('count'):
                priority = 10
            if group is None or group['count'] == 0:
                continue
            semantic_id = 'vasp.force.maximum_atomic_norm.' + scope
            def metric(record):
                summary = record['summary'][scope]
                definition = 'Maximum Euclidean atomic force-vector norm over ' + scope + ' atoms; not maximum component'
                criterion = None
                qualification = 'derived'
                if scope != 'all':
                    qualification = 'current-input-derived'
                    definition += '; whole-atom groups from current POSCAR, historical binding unestablished'
                if (scope == 'free' and not record['summary']['partial']
                        and _scope(self.out)['calculation'] == 'fixed-cell-relaxation'):
                    criterion = self._criterion('EDIFFG', 'eV/Angstrom',
                                                'Conditional free-atom criterion using current POSCAR flags; historical binding unestablished')
                    qualification = 'provisional'
                value = summary['maximum_norm']
                bound = math.sqrt(3) * record['precision']
                parsed = {'value': value, 'half_last_place': bound}
                relation = _relation(parsed, criterion)
                if relation and not relation.startswith('PROVISIONAL_'):
                    relation = 'PROVISIONAL_' + relation
                return Metric(semantic_id, 'Fmax ' + scope, definition, 'eV/Angstrom', value,
                              self._levels(record['evaluation']), self.context.stage,
                              self.out_segment, bound, None, None, qualification,
                              ((record['evidence'], record['constraint_evidence']) if record['constraint_evidence'] else (record['evidence'],)), record['at'], criterion,
                              self._targets(semantic_id), relation, priority,
                              comparison_context=(), comparison_scope=repr(record['flags_key']))
            previous = None
            if len(records) > 1:
                candidate = records[-2]
                if candidate['flags_key'] == latest['flags_key'] and candidate['summary'].get(scope) and candidate['summary'][scope]['count']:
                    previous = metric(candidate)
            latest_metric = metric(latest)
            result.append(MetricSeries(latest_metric, previous if previous is not None and comparable(latest_metric, previous) else None))
        return result

    @staticmethod
    def _history_columns():
        return (HistoryColumn('native_label', 'EVAL / NATIVE'),
                HistoryColumn('child_count', 'SCF COUNT', kind='count', kinds=('evaluation', 'trial-evaluation')),
                HistoryColumn('force', 'FMAX FREE', 'vasp.force.maximum_atomic_norm.free', 'metric', kinds=('evaluation', 'trial-evaluation')),
                HistoryColumn('dE', '|dE| eV', 'vasp.electronic.dE', 'metric', kinds=('electronic',)),
                HistoryColumn('d_eps', '|d eps| eV', 'vasp.electronic.d_eps', 'metric', kinds=('electronic',)),
                HistoryColumn('rms_c', 'rms(c)', 'vasp.electronic.rms_c', 'metric', kinds=('electronic',)),
                HistoryColumn('duration', 'DURATION / SOURCE', kind='timing'),
                HistoryColumn('state', 'STATE'),
                HistoryColumn('annotations', 'ELECTRONIC EVIDENCE', kind='evidence'))

    def _record_timings(self, record, levels, *, finished):
        timer = 'LOOP+' if len(levels) == 1 else 'LOOP'
        prefix = 'vasp.' + ('evaluation' if len(levels) == 1 else 'electronic')
        start = record['line'].evidence
        scope = _scope(self.out)
        if record['timings'] and not record['timing_issue'] and scope['supported']:
            return tuple(Timing(prefix + '.' + kind, timer + ' ' + kind,
                parsed['value'], clock=kind, source='native', context=levels,
                stage=self.context.stage, segment=self.out_segment, completeness='COMPLETE',
                evidence=(line.evidence,), start_evidence=(start,),
                end_evidence=(line.evidence,), precision=parsed['half_last_place'],
                reason=('Inclusive native outer-loop interval; do not add nested LOOP timers; '
                        'preparation outside the interval is not measured' if timer == 'LOOP+' else
                        'Native electronic-loop interval inside this Iteration header'))
                for kind, parsed, line in record['timings'])
        mono, historical, clock_id = self.observation
        if not historical and mono is not None and not finished:
            if record['observer_start'] is None:
                record['observer_start'], record['observer_clock'] = mono, clock_id
        first = record['observer_start']
        if first is not None and finished and record['observer_end'] is None and not historical:
            record['observer_end'] = mono
        last = record['observer_end'] if finished else mono
        if (not historical and first is not None and last is not None and last > first
                and clock_id == record['observer_clock']):
            return (Timing(prefix + '.observation', 'Observed context interval', last-first,
                source='observer', quantity='observation-interval', context=levels,
                stage=self.context.stage, segment=self.out_segment, completeness='BOUNDED',
                start_evidence=(start,), end_evidence=((record.get('end_line') or record['line']).evidence,),
                lower_bound=last-first, upper_bound=last-first, clock_id=clock_id,
                reason=((record['timing_issue'] + '; ') if record['timing_issue'] else '') +
                       'Within-session observation interval only; buffered output and an unknown start prevent an execution duration'),)
        reason = record['timing_issue'] or ('Native timing unavailable; historical execution duration cannot be recovered'
                  if finished else 'Native timing pending; execution start is unknown')
        if first is not None and last is not None and (last < first or clock_id != record['observer_clock']):
            reason += '; observer clock reset or changed scope makes the interval invalid'
        if not scope['supported']:
            reason = 'Native timing scope is outside supported ordinary static/fixed-cell VASP'
        return (Timing(prefix + '.wall', timer + ' wall', context=levels,
                       stage=self.context.stage, segment=self.out_segment, reason=reason,
                       evidence=(start,)),)

    def _put_history(self, record, *, levels=(), parent_id=None, kind='evaluation',
                     native_label='', state='PARTIAL', coverage='PARTIAL', metrics=(),
                     timings=(), evidence=(), child_count=0, annotations=(), segment=None):
        entry = HistoryEntry(record['history_id'], record['history_sequence'],
            context=levels, parent_id=parent_id, kind=kind, native_label=str(native_label),
            state=state, coverage=coverage, metrics=tuple(metrics), timings=tuple(timings),
            evidence=tuple(evidence), binding_id=self.context.binding_id,
            attempt_id=self.context.attempt_id, stage=self.context.stage,
            segment=segment or self.out_segment, child_count=child_count, annotations=tuple(annotations))
        self.history_updates[entry.entry_id] = entry

    def _publish_local_history(self):
        stream = self.streams.get(self.electronic_role)
        if stream is None or self.asymmetric_reset:
            return
        for item in stream.evaluations:
            rows = self._usable_rows(item)
            if not rows:
                continue
            key = (item['segment'], item['index'])
            native = (next((entry for entry in self.out['evaluations'] if entry['index'] == item['index']), None)
                      if self.out and item['segment'] == self.out_segment else None)
            if self.out and native is None:
                # Future sequence rows stay available in Snapshot/Details/Copy.
                # Publishing a guessed parent here would break chronology while
                # a previous native evaluation is still arriving.
                continue
            if native is not None:
                # Native history already owns this context unless local rows
                # existed first. Keep its established identity when peers arrive.
                if key not in self.local_history:
                    continue
                state, matched, _ = self._correspondence(native)
                if matched and state not in {'conflict', 'unresolved', 'unsupported'}:
                    continue
            record = self.local_history.get(key)
            if record is None:
                record = {'rows': {}, 'segment': item['segment']}
                self._history_identity(record, rows[0]['line'], 'electronic-series')
                self.local_history[key] = record
                while len(self.local_history) > MAX_EVALUATIONS:
                    self.local_history.pop(next(iter(self.local_history)))
            metrics = tuple(series.latest for series in self._electronic_metrics({}, rows, associated=False,
                            qualification='source-local', segment=item['segment']))
            if self.out and item['segment'] != self.out_segment:
                metrics = tuple(replace(metric, criterion=None, relation=None) for metric in metrics)
            conflicts = tuple(message for segment, message, _ in self.stream_conflicts if segment == item['segment'])
            notes = ('Complete source-local electronic sequence; native outer-evaluation association unestablished', *conflicts)
            if conflicts:
                metrics = tuple(replace(metric, criterion=None, relation=None, qualification='conflict') for metric in metrics)
            self._put_history(record, kind='electronic-series', native_label='Source-local SCF',
                state='PARTIAL', coverage='PARTIAL', metrics=metrics, child_count=len(rows),
                evidence=(rows[0]['line'].evidence,), annotations=notes, segment=item['segment'])
            for row in rows:
                child = record['rows'].get(row['label'])
                if child is None:
                    child = {}
                    self._history_identity(child, row['line'], 'electronic')
                    child['history_id'] = record['history_id'] + ':electronic:' + str(row['label'])
                    record['rows'][row['label']] = child
                row_metrics = tuple(series.latest for series in self._electronic_metrics({}, (row,), associated=False,
                                    qualification='source-local', segment=item['segment']))
                if self.out and item['segment'] != self.out_segment:
                    row_metrics = tuple(replace(metric, criterion=None, relation=None) for metric in row_metrics)
                if conflicts:
                    row_metrics = tuple(replace(metric, criterion=None, relation=None, qualification='conflict') for metric in row_metrics)
                self._put_history(child, levels=row_metrics[0].context, parent_id=record['history_id'],
                    kind='electronic', native_label=row['label'], state='COMPLETE', coverage='PARTIAL',
                    metrics=row_metrics, evidence=(row['line'].evidence,), annotations=notes, segment=item['segment'])

    def _publish_evaluation(self, evaluation):
        self.history_dirty.discard(evaluation['serial'])
        correspondence, matched, message = self._correspondence(evaluation)
        local = self.local_history.get((self.out_segment, evaluation['index']))
        if local is not None and not evaluation.get('local_history_adopted'):
            if not matched or correspondence in {'conflict', 'unresolved', 'unsupported'}:
                return
            evaluation['history_id'] = local['history_id']
            evaluation['history_sequence'] = local['history_sequence']
            evaluation['local_history_adopted'] = True
            for row in evaluation['rows']:
                row['history_id'] = evaluation['history_id'] + ':electronic:' + str(row['label'])
                old = local['rows'].get(row['label'])
                if old is not None:
                    row['history_sequence'] = old['history_sequence']
        supported = _scope(self.out)['supported']
        electronic_conflicts = [message for segment, message, _ in self.stream_conflicts if segment == self.out_segment]
        coverage = ('UNRESOLVED' if electronic_conflicts or self.out['conflicts'] or correspondence in {'conflict', 'unresolved'} else
                    'COMPLETE' if correspondence == 'matched' and supported else 'PARTIAL')
        levels = self._levels(evaluation)
        notes = [*self.out['conflicts'], *electronic_conflicts]
        if not supported:
            notes.extend(_scope(self.out)['reasons'])
        if message:
            notes.append(message)
        if evaluation['native_convergence'] is not None:
            notes.append('Native EDIFF reached' if evaluation['native_convergence'] else 'Native EDIFF not reached')
        if evaluation['trial']:
            notes.append('Native trial/line-search text observed; no accepted optimizer step inferred')
        notes.append('Accepted optimization step unestablished')
        force = evaluation['force_record']
        metrics = [item.latest for item in self._force_metrics((force,))] if force else []
        if matched and correspondence not in {'conflict', 'unresolved', 'unsupported'}:
            metrics.extend(item.latest for item in self._electronic_metrics(evaluation, matched) if item.latest.semantic_id != 'vasp.electronic.free_energy')
        for key, value in evaluation['energies'].items():
            metrics.append(Metric('vasp.evaluation.' + key, key.replace('_', ' '),
                'Native evaluated ionic energy; distinct from electronic iteration energy',
                'eV', value['value'], levels, self.context.stage, self.out_segment,
                value['half_last_place'], value['printed'], evidence=(evaluation['energy_refs'][key],)))
        if not supported or self.out['conflicts']:
            metrics = [replace(metric, qualification='provisional', criterion=None, relation=None) for metric in metrics]
        if electronic_conflicts:
            metrics = [replace(metric, qualification='conflict', criterion=None, relation=None)
                       if metric.semantic_id.startswith('vasp.electronic.') else metric for metric in metrics]
        source_item = next((item for item in self.osz if item['segment'] == self.out_segment and item['index'] == evaluation['index']), None)
        child_count = max(len(evaluation['rows']), len(self._usable_rows(source_item)) if source_item and matched else 0)
        complete = evaluation['complete']
        self._put_history(evaluation, levels=levels,
            kind='trial-evaluation' if evaluation['trial'] else 'evaluation',
            native_label=f"{evaluation['index']} / {evaluation['native']}",
            state='COMPLETE' if complete else 'INCOMPLETE' if evaluation['closed'] else 'PARTIAL', coverage=coverage,
            metrics=metrics, timings=self._record_timings(evaluation, levels, finished=complete or evaluation['closed']),
            evidence=(evaluation['line'].evidence, *((force['evidence'],) if force else ()),
                      *evaluation['trial']), child_count=child_count, annotations=notes)
        source_item = next((item for item in self.osz if item['segment'] == self.out_segment and item['index'] == evaluation['index']), None)
        usable = self._usable_rows(source_item) if source_item else []
        associated = bool(matched) and correspondence not in {'conflict', 'unresolved', 'unsupported'}
        by_label = {row['label']: row for row in usable} if associated else {}
        for index, row in enumerate(evaluation['rows']):
            row_levels = self._levels(evaluation, row['label'])
            native_energy = row['energy']
            row_metrics = []
            if native_energy is not None:
                row_metrics.append(Metric('vasp.electronic.free_energy', 'Electronic energy',
                    'Native electronic iteration free energy; not evaluated ionic energy', 'eV',
                    native_energy['value'], row_levels, self.context.stage, self.out_segment,
                    native_energy['half_last_place'], native_energy['printed'],
                    evidence=(row['energy_ref'],)))
            counterpart = by_label.get(row['label'])
            if counterpart is not None and correspondence not in {'conflict', 'unresolved', 'unsupported'}:
                qualification = 'row-correspondence' if index < len(matched) else 'source-local'
                row_metrics.extend(item.latest for item in self._electronic_metrics(evaluation, (counterpart,), qualification=qualification)
                                   if item.latest.semantic_id != 'vasp.electronic.free_energy' or native_energy is None)
            if not supported or self.out['conflicts'] or correspondence == 'conflict':
                row_metrics = [replace(metric, qualification='provisional', criterion=None, relation=None) for metric in row_metrics]
            if electronic_conflicts:
                row_metrics = [replace(metric, qualification='conflict', criterion=None, relation=None) for metric in row_metrics]
            finished = native_energy is not None or index < len(evaluation['rows']) - 1 or complete or evaluation['closed']
            self._put_history(row, levels=row_levels, parent_id=evaluation['history_id'],
                kind='electronic', native_label=row['label'], state='COMPLETE' if finished else 'PARTIAL',
                coverage='COMPLETE' if not electronic_conflicts and counterpart is not None and index < len(matched) and supported and not self.out['conflicts'] and correspondence not in {'conflict', 'unresolved', 'unsupported'} else 'PARTIAL',
                metrics=row_metrics, timings=self._record_timings(row, row_levels, finished=finished),
                evidence=(row['line'].evidence,), annotations=((*electronic_conflicts, message) if message else tuple(electronic_conflicts)))

        if associated:
            native_labels = {row['label'] for row in evaluation['rows']}
            for counterpart in usable:
                if counterpart['label'] in native_labels:
                    continue
                key = (evaluation['history_id'], counterpart['label'])
                record = self.pending_electronic_history.get(key)
                if record is None:
                    record = {'line': counterpart['line'], 'timings': (), 'timing_issue': None}
                    self._history_identity(record, counterpart['line'], 'electronic')
                    record['history_id'] = evaluation['history_id'] + ':electronic:' + str(counterpart['label'])
                    self.pending_electronic_history[key] = record
                row_levels = self._levels(evaluation, counterpart['label'])
                self._put_history(record, levels=row_levels, parent_id=evaluation['history_id'],
                    kind='electronic', native_label=counterpart['label'], state='COMPLETE', coverage='PARTIAL',
                    metrics=tuple(replace(series.latest, qualification='conflict', criterion=None, relation=None)
                                  if electronic_conflicts else series.latest
                                  for series in self._electronic_metrics(evaluation, (counterpart,), qualification='source-local')),
                    evidence=(counterpart['line'].evidence,),
                    annotations=('Complete source-local row; OUTCAR counterpart not yet written', *electronic_conflicts))

    def _publish_history(self):
        self._publish_local_history()
        if self.out is None:
            return
        for evaluation in self.out['evaluations']:
            if evaluation['serial'] in self.history_dirty or not (evaluation['complete'] or evaluation['closed']):
                self._publish_evaluation(evaluation)
                self.history_dirty.discard(evaluation['serial'])
        if not self.segment_dirty:
            return
        self.segment_dirty = False
        totals = []
        for label, clock in (('Total CPU time used', 'cpu'), ('Elapsed time', 'wall')):
            value_line = self.out['footer_timings'].get(label)
            if value_line is not None:
                value, line = value_line
                valid = value['value'] is not None and value['value'] >= 0
                totals.append(Timing('vasp.segment.' + clock, label,
                    value['value'] if valid else None, clock=clock, quantity='cumulative',
                    source='native' if valid else 'unavailable', stage=self.context.stage,
                    segment=self.out_segment, completeness='COMPLETE' if valid else 'UNAVAILABLE',
                    evidence=(line.evidence,), reason='Whole native run total; never apportioned to evaluations or added to LOOP/LOOP+'))
        self._put_history(self.out, kind='segment-total', native_label=f'Segment {self.out_segment}',
            state='COMPLETE' if len(self.out['timing']) == 4 else 'PARTIAL',
            coverage='COMPLETE' if len(self.out['timing']) == 4 else 'PARTIAL',
            timings=totals, evidence=(self.out['line'].evidence,),
            annotations=('Native segment boundary; preparation/outside-loop duration unavailable',))

    def _completed_durations(self):
        if self.out is None:
            return ()
        evaluations = [item for item in self.out['evaluations'] if item['complete']][-2:]
        result = []
        for item in reversed(evaluations):
            timings = self._record_timings(item, self._levels(item), finished=True)
            result.append(next(value for value in timings if value.clock == 'wall'))
        return tuple(result)

    def update(self, updates, now):
        # Each role has its own reader and bounded queue. Rotate fairly across
        # streams at the same evaluation frontier, so a small stdout cannot
        # consume all retained history while a large OUTCAR is catching up.
        for update in sorted(updates, key=lambda u: u.source.role.lower() != 'poscar'):
            role = update.source.role.lower()
            self.states[role] = update.state
            prior = self.generations.get(role)
            self.generations[role] = update.state.generation
            if update.reset and prior is not None and prior > 0 and role in {'outcar', 'oszicar', 'stdout'}:
                self.asymmetric_reset = True
                self.history_updates.clear()
                self.pending_lines.pop(role, None)
                self.pending_electronic_history.clear()
                self.local_history.clear()
                if role == 'outcar':
                    self.out = None
                    self.out_segment = 0
                else:
                    self.streams[role] = _ElectronicStream()
                self.stream_conflicts = deque((entry for entry in self.stream_conflicts if role not in entry[2]), maxlen=32)
                self._issue('Source reset: ' + str(update.reason or role))
            if role == 'poscar':
                self._read_poscar(update)
            elif role == 'incar':
                self._read_incar(update)
            elif role == 'outcar' or role in self.streams:
                self.pending_lines.setdefault(role, deque()).extend(update.lines)
        poscar_pending = bool(self.states.get('poscar') and self.states['poscar'].has_more)
        history_full = len(self.history_updates) >= MAX_PENDING_HISTORY
        if history_full:
            self._issue('History consumer must drain pending entries before reading more')
        for _ in range(0 if poscar_pending or history_full else MAX_HISTORY_LINES):
            ready = [role for role, lines in self.pending_lines.items() if lines]
            if not ready:
                break
            serial = lambda role: self.out_serial if role == 'outcar' else self.streams[role].serial
            role = min(ready, key=lambda role: (serial(role), role != 'outcar', role))
            waiting = [name for name, state in self.states.items() if name != role
                       and (name == 'outcar' or name in self.streams) and state.has_more and not self.pending_lines.get(name)]
            if any(serial(name) < serial(role) for name in waiting):
                break
            line = self.pending_lines[role].popleft()
            if role == 'outcar':
                self._read_out(line)
            else:
                self._read_electronic(line, self.streams[role])
        self._select_electronic()
        self._check_all_streams()
        electronic_metrics, verification, local_iteration = self._selected_observation()
        messages, limitations = [], list(self.context.limitations)
        limitations.extend(self.issues)
        limitations.extend(('Live observations never establish scientific acceptance or process ownership',
                            'Evaluations are chronological; accepted optimizer steps are not inferred'))
        limitations.extend(self.incar_issues)
        for setting in self._settings():
            if setting.discrepancy:
                limitations.append(f'{setting.name}: declared INCAR differs from effective OUTCAR; effective OUTCAR remains authoritative')
        if self.flags is not None:
            limitations.append('Constraint groups are current-POSCAR-derived; historical execution binding is unestablished')
        elif self.poscar_issue:
            limitations.append(self.poscar_issue)
        metrics, iteration = [], ()
        status, phase, calculation, completeness = 'PENDING', 'Awaiting native evidence', None, 'PENDING'
        if self.out is not None:
            scope = _scope(self.out)
            calculation = scope['calculation']
            evaluation = self.out['current']
            if evaluation:
                electronic = evaluation['rows'][-1]['label'] if evaluation['rows'] else None
                iteration = self._levels(evaluation, electronic)
                phase = 'Electronic SCF' if evaluation['force'] is None else 'Force evaluation observed'
                status, completeness = 'OBSERVED', 'PARTIAL'
                correspondence, rows, message = self._correspondence(evaluation)
                if message:
                    messages.append(message)
                if correspondence == 'conflict':
                    status = 'SOURCE_CONFLICT'
                elif correspondence == 'unresolved':
                    status = 'SOURCE_UNRESOLVED'
                elif correspondence == 'unsupported':
                    status = 'UNSUPPORTED'
                metrics.extend(self._force_metrics())
                ahead = self.osz_current
                active_ahead = (ahead is not None and ahead['segment'] == self.out_segment
                                and ahead['index'] > evaluation['index'] and bool(ahead['rows']))
                if (ahead is not None and ahead['segment'] == self.out_segment
                        and ahead['index'] > evaluation['index'] and ahead['rows']):
                    iteration = local_iteration
                    phase = 'Electronic SCF; native counterpart pending'
                    metrics = [item for item in metrics if not item.latest.semantic_id.startswith('vasp.electronic.')]
                    messages.append(f"Evaluation {ahead['index']} force not yet available; OUTCAR correspondence pending")
                if evaluation['force'] is None:
                    messages.append(f"Evaluation {evaluation['index']} force not yet available")
                if evaluation['complete'] and correspondence == 'matched' and not active_ahead:
                    completeness = 'COMPLETE_EVALUATION'
                if evaluation['native_convergence'] is not None:
                    messages.append('Native EDIFF reached' if evaluation['native_convergence'] else 'Native EDIFF not reached')
                if not scope['supported']:
                    status = 'UNSUPPORTED'
                    limitations.extend(scope['reasons'])
                    metrics = [MetricSeries(replace(item.latest, criterion=None, relation=None, qualification='provisional'),
                                            replace(item.previous, criterion=None, relation=None, qualification='provisional') if item.previous else None)
                               for item in metrics]
            if self.out['conflicts']:
                status = 'SOURCE_CONFLICT'
                metrics = [MetricSeries(replace(item.latest, criterion=None, relation=None, qualification='provisional'),
                                        replace(item.previous, criterion=None, relation=None, qualification='provisional') if item.previous else None)
                           for item in metrics]
                limitations.extend(self.out['conflicts'])
            if self.out['force'] is not None:
                messages.append('Force table pending its complete rows and closing separator')
            values = self._values()
            if values.get('EDIFF') == 0:
                messages.append('EDIFF=0: fixed-count behavior; no ordinary SCF threshold')
            elif type(values.get('EDIFF')) in (int, float) and values['EDIFF'] < 0:
                messages.append('Negative EDIFF unsupported; no ordinary SCF threshold')
            elif type(values.get('EDIFF')) not in (int, float):
                messages.append('Effective EDIFF unavailable; no SCF target inferred')
            ediffg = values.get('EDIFFG')
            if type(ediffg) in (int, float):
                if ediffg > 0:
                    messages.append('Positive EDIFFG is energy-based; no force target')
                elif ediffg == 0:
                    messages.append('EDIFFG=0: step-budget behavior; no force target')
            if self.out['fatal'] or self.out['interrupted']:
                phase = 'Native interruption evidence'
            elif len(self.out['timing']) == 4:
                phase = 'Native timing footer observed'
            if self.out['ionic_stop']:
                messages.append('Native optimizer stopping observed; no accepted-step count inferred')
        if self.flags is not None and any(any(row) and not all(row) for row in self.flags):
            limitations.append('Partial-direction constraints are not Cartesian masks; projected/free-force criterion unsupported')
        if electronic_metrics:
            metrics.extend(electronic_metrics)
            if not iteration or electronic_metrics[0].latest.segment > max(1, self.out_segment):
                iteration = local_iteration
                phase, status, completeness = 'Electronic SCF; native association pending', 'OBSERVED', 'PARTIAL'
        active_segment = max(1, self.out_segment, *(series.latest.segment for series in electronic_metrics))
        conflicts = [message for segment, message, _ in self.stream_conflicts if segment == active_segment]
        if conflicts:
            status = 'SOURCE_CONFLICT'
            limitations.extend(conflicts)
        elif self.asymmetric_reset:
            status = 'SOURCE_UNRESOLVED'
        if self.out and (self.out['conflicts'] or not _scope(self.out)['supported']):
            metrics = [MetricSeries(replace(item.latest, criterion=None, relation=None, qualification='provisional'),
                replace(item.previous, criterion=None, relation=None, qualification='provisional') if item.previous else None) for item in metrics]
        settings = self._settings()
        if self.out and active_segment != self.out_segment:
            calculation = None
            settings = tuple(replace(setting, qualification='other-segment',
                origin=setting.origin + f' (native segment {self.out_segment}; active source segment {active_segment})')
                if setting.qualification == 'effective' else setting for setting in settings)
        self._publish_history()
        snapshot = ProgressSnapshot(self.context, 'vasp-live-6.6.1', status=status, phase=phase,
                                calculation=calculation, segment=active_segment,
                                iteration=iteration, metrics=tuple(sorted(metrics, key=lambda item: item.latest.priority)),
                                events=tuple(self.events), completeness=completeness,
                                history_columns=self._history_columns(),
                                completed_durations=self._completed_durations(),
                                last_new_evidence_at=self.last_new, messages=tuple(messages),
                                verification=verification, settings=settings,
                                limitations=tuple(dict.fromkeys(limitations)))
        from .vasp_card import vasp_card_groups
        return replace(snapshot, card_groups=vasp_card_groups(snapshot))
