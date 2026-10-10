"""Bounded incremental VASP 6.6.1 evidence, never scientific acceptance.

Numerical token, printed-resolution, mode and force definitions are shared with
result inspection. This adapter owns only live block/correspondence state; it has
no Jobs store, process, launcher or finalization interface.
"""
from __future__ import annotations

from collections import deque
from dataclasses import replace
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
                     ProgressEvent, ProgressSnapshot, comparable)

MAX_EVALUATIONS = 16
MAX_ITERATIONS = 1024
MAX_ATOMS = 10000
MAX_POSCAR_BYTES = 1024 * 1024


def _batch_line(line):
    ref = line.evidence
    return SimpleNamespace(text=line.text, line_number=ref.line_start,
                           byte_start=ref.byte_start, byte_end=ref.byte_end)


def _span(first, last):
    return replace(first, line_end=last.line_end, byte_end=last.byte_end)


def _relation(parsed, criterion):
    if criterion is None:
        return None
    return {'PASS': 'BELOW', 'FAIL': 'AT_OR_ABOVE', 'UNKNOWN': 'UNCERTAIN',
            'NOT_APPLICABLE': 'NOT_APPLICABLE'}[below_printed(parsed, criterion.value)]


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
        self.osz_segment = 1
        self.osz_banner_seen = False
        self.out = None
        self.osz = deque()
        self.osz_next = 1
        self.osz_current = None
        self.osz_last_native = None
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
        self.electronic_role = self._electronic_role()
        self.duplicate_streams = sum(s.role.lower() in {'oszicar', 'stdout'}
                                     for s in context.sources) > 1

    def _electronic_role(self):
        roles = {s.role.lower() for s in self.context.sources}
        return 'oszicar' if 'oszicar' in roles else 'stdout' if 'stdout' in roles else None

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
        self.out_segment += 1
        self.out = {'version': version, 'settings': {}, 'echoed_incar': {},
                    'in_echo': False, 'nions': None, 'evaluations': deque(),
                    'next': 1, 'current': None, 'force': None,
                    'forces': deque(maxlen=2), 'conflicts': [], 'in_energy': False,
                    'fatal': False, 'interrupted': False, 'footer': False,
                    'timing': set(), 'ionic_stop': False}
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
                evaluation = {'index': out['next'], 'native': ionic, 'rows': [],
                              'energies': {}, 'force': None, 'complete': False,
                              'native_convergence': None}
                out['next'] += 1
                if len(out['evaluations']) == MAX_EVALUATIONS:
                    dropped = out['evaluations'].popleft()
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
                evaluation['rows'].append({'label': electronic, 'energy': None,
                                           'algorithm': None, 'line': line})
            out['force'] = None
            out['in_energy'] = False
            self._event('iteration', line, 'Native electronic iteration observed',
                        self._levels(evaluation, electronic))
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
            else:
                evaluation['rows'][-1]['energy'] = parsed
            self.last_new = line.observed_at
        energies = re.search(r'energy\s+without entropy\s*=\s*(' + NUMBER + r').*energy\(sigma->0\)\s*=\s*(' + NUMBER + ')', text, re.I)
        if energies and evaluation and out['in_energy']:
            evaluation['energies'].update(zip(('without_entropy', 'sigma_to_zero'),
                                              (number(t) for t in energies.groups())))
            evaluation['complete'] = (evaluation['force'] is not None and
                                      evaluation['energies'].get('free_energy', {}).get('value') is not None and
                                      all(v['value'] is not None for v in evaluation['energies'].values()))
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
            if len(out['timing']) == 4:
                self._event('native-footer', line, 'Complete native timing footer observed')
        if re.search(r'VERY BAD NEWS|internal error|ERROR FEXCP|ZHEGV failed|ERROR EDD|killed by signal', text, re.I):
            out['fatal'] = True
            self._event('native-fatal', line, 'Native fatal/interruption text observed')
        if re.search(r'LSTOP\s*=\s*T|LABORT\s*=\s*T|STOPCAR.*(?:stop|found)|soft stop', text, re.I):
            out['interrupted'] = True
            self._event('native-stop-request', line, 'Native stop-request text observed')

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
        self.out['forces'].append(record)
        self._event('force', line, 'Complete atomic-vector force block observed',
                    self._levels(block['evaluation']))

    def _new_osz_evaluation(self):
        item = {'index': self.osz_next, 'segment': self.osz_segment, 'rows': [], 'summary': None}
        self.osz_next += 1
        if len(self.osz) >= MAX_EVALUATIONS:
            dropped = self.osz.popleft()
            if self.out is not None and self.out['current'] and dropped['segment'] >= self.out_segment and dropped['index'] >= self.out['current']['index']:
                self._issue('Unmatched electronic evaluation buffer limit reached')
        self.osz.append(item)
        self.osz_current = item
        return item

    def _read_electronic(self, line):
        if BANNER.match(line.text):
            if self.osz_banner_seen or self.osz:
                self.osz_segment += 1
            self.osz_banner_seen = True
            self.osz_next = 1
            self.osz_current = None
            self.osz_last_native = None
            return
        row = parse_electronic_row(_batch_line(line), line.evidence.source_role)
        if row is not None:
            if self.osz_current is None or (row['label'] == 1 and self.osz_current['rows']):
                self._new_osz_evaluation()
            item = self.osz_current
            if len(item['rows']) >= MAX_ITERATIONS:
                self._issue('Electronic source row buffer limit reached')
                return
            row['line'] = line
            item['rows'].append(row)
            self.last_new = line.observed_at
        summary = parse_ionic_summary(_batch_line(line), line.evidence.source_role)
        if summary is not None:
            item = self.osz_current or self._new_osz_evaluation()
            # OSZICAR has no mandatory banner. Native summary resets establish
            # the batch parser's segment boundary only after that summary arrives.
            if self.osz_last_native is not None and summary['native_ionic_label'] <= self.osz_last_native:
                self.osz_segment += 1
                item['segment'] = self.osz_segment
                item['index'] = 1
                self.osz_next = 2
            item['summary'] = summary
            self.osz_last_native = summary['native_ionic_label']
            self.osz_current = None
            self.last_new = line.observed_at

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

    def _correspondence(self, evaluation):
        if self.asymmetric_reset:
            return 'unresolved', [], 'Source generations changed independently; sibling correspondence is unestablished'
        item = next((v for v in self.osz if v['segment'] == self.out_segment and v['index'] == evaluation['index']), None)
        if item is None and self.osz_segment > self.out_segment:
            return 'unresolved', [], 'Electronic source segment cannot be associated with the current native segment'
        if item is None:
            return 'pending', [], 'Awaiting electronic counterpart for the native evaluation'
        native_rows, rows = evaluation['rows'], item['rows']
        if self.out['evaluations'] and evaluation['index'] < self.out['evaluations'][0]['index']:
            return 'unresolved', [], 'Native correspondence buffer was exceeded'
        matched = []
        for native, row in zip(native_rows, rows):
            if native['label'] != row['label'] or row.get('malformed'):
                return 'conflict', [], 'Complete electronic rows disagree in label or grammar'
            if row['algorithm'] not in {'DAV', 'RMM'}:
                return 'unsupported', [], 'Electronic algorithm is outside DAV/RMM support'
            if native['algorithm'] is not None and native['algorithm'] != row['algorithm']:
                return 'conflict', [], 'Electronic algorithms conflict across files'
            if native['energy'] is None:
                break
            if not _same_printed(native['energy'], row.get('energy')):
                return 'conflict', [], 'Complete electronic energies conflict across files'
            matched.append(row)
        summary = item['summary']
        if summary is not None:
            if summary['native_ionic_label'] != evaluation['native']:
                return 'conflict', [], 'Native evaluation labels conflict across files'
            for key in ('free_energy', 'sigma_to_zero'):
                actual = evaluation['energies'].get(key)
                if actual is not None and not _same_printed(actual, summary[key]):
                    return 'conflict', [], 'Complete evaluated ionic energies conflict across files'
            if evaluation['complete'] and len(native_rows) != len(rows):
                return 'conflict', [], 'Completed evaluation electronic row counts conflict'
        if summary is None and evaluation['complete']:
            return 'pending', matched, 'Awaiting evaluated-energy counterpart; matched SCF rows remain usable'
        if len(matched) < max(len(native_rows), len(rows)) or (summary is not None and not evaluation['complete']):
            return 'pending', matched, 'Cross-file evidence is pending; complete matching rows remain usable'
        return 'matched', matched, ''

    def _criterion(self, key, unit, definition):
        value = self._values().get(key)
        if type(value) not in (int, float) or not math.isfinite(value):
            return None
        if key == 'EDIFF' and value > 0:
            return Criterion(value, unit, '<', 'OUTCAR effective EDIFF', definition=definition,
                             signed_value=value)
        if key == 'EDIFFG' and value < 0:
            return Criterion(-value, unit, '<', 'OUTCAR effective EDIFFG', definition=definition,
                             signed_value=value)
        return None

    def _targets(self, semantic_id):
        return tuple(v.criterion for v in self.context.diagnostic_targets if v.semantic_id == semantic_id)

    def _electronic_metrics(self, evaluation, rows):
        result = []
        if not rows:
            return result
        for key, label, definition, unit, magnitude, priority in (
            ('dE', '|dE|', 'Magnitude of electronic total-energy change within one SCF evaluation', 'eV', True, 20),
            ('d_eps', '|d eps|', 'Magnitude of electronic band-energy change within one SCF evaluation', 'eV', True, 21),
            ('rms', 'rms', 'Native electronic wavefunction residual norm', 'native', False, 40),
            ('rms_c', 'rms(c)', 'Native charge-density residual norm', 'native', False, 30),
        ):
            semantic_id = 'vasp.electronic.' + key
            criterion = self._criterion('EDIFF', 'eV', 'Both dE and d eps magnitudes must satisfy effective EDIFF') if magnitude else None
            def metric(row):
                value = row.get(key)
                # A missing current rms(c) stays unavailable; do not promote an older value.
                parsed = value or {'value': None, 'half_last_place': None, 'printed': None}
                raw = parsed['value']
                return Metric(semantic_id, label, definition, unit,
                              abs(raw) if magnitude and raw is not None else raw,
                              self._levels(evaluation, row['label']), self.context.stage,
                              self.out_segment, parsed['half_last_place'], parsed['printed'],
                              raw if magnitude else None, 'observed', (row['line'].evidence,),
                              row['line'].observed_at, criterion, self._targets(semantic_id),
                              _relation(parsed, criterion), priority,
                              comparison_context=self._levels(evaluation))
            latest = metric(rows[-1])
            previous = metric(rows[-2]) if len(rows) > 1 and rows[-2]['label'] + 1 == rows[-1]['label'] else None
            result.append(MetricSeries(latest, previous if previous is not None and comparable(latest, previous) else None))
        return result

    def _force_metrics(self):
        if not self.out['forces']:
            return []
        result = []
        for scope, priority in (('all', 50), ('free', 10), ('fixed', 51)):
            latest = self.out['forces'][-1]
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
                if relation:
                    relation = 'PROVISIONAL_' + relation
                return Metric(semantic_id, 'Fmax ' + scope, definition, 'eV/Angstrom', value,
                              self._levels(record['evaluation']), self.context.stage,
                              self.out_segment, bound, None, None, qualification,
                              ((record['evidence'], record['constraint_evidence']) if record['constraint_evidence'] else (record['evidence'],)), record['at'], criterion,
                              self._targets(semantic_id), relation, priority,
                              comparison_context=(), comparison_scope=repr(record['flags_key']))
            previous = None
            if len(self.out['forces']) > 1:
                candidate = self.out['forces'][-2]
                if candidate['flags_key'] == latest['flags_key'] and candidate['summary'].get(scope) and candidate['summary'][scope]['count']:
                    previous = metric(candidate)
            latest_metric = metric(latest)
            result.append(MetricSeries(latest_metric, previous if previous is not None and comparable(latest_metric, previous) else None))
        return result

    def update(self, updates, now):
        # Constraints are parsed before force blocks from the same observation,
        # but never retroactively substituted into earlier force identities.
        for update in sorted(updates, key=lambda u: u.source.role.lower() != 'poscar'):
            role = update.source.role.lower()
            self.states[role] = update.state
            prior = self.generations.get(role)
            self.generations[role] = update.state.generation
            if update.reset and prior is not None and prior > 0 and role in {'outcar', 'oszicar', 'stdout'}:
                self.asymmetric_reset = True
                if role == 'outcar':
                    self.out = None
                    self.out_segment = 0
                elif role == self.electronic_role:
                    self.osz.clear()
                    self.osz_current = None
                    self.osz_segment, self.osz_next = 1, 1
                    self.osz_last_native = None
                self._issue('Source reset: ' + str(update.reason or role))
            if role == 'poscar':
                self._read_poscar(update)
                continue
            for line in update.lines:
                if role == 'outcar':
                    self._read_out(line)
                elif role == self.electronic_role:
                    self._read_electronic(line)
        if self.out is not None:
            # Late counterparts may prove a contradiction in an earlier retained
            # evaluation. A later valid evaluation cannot erase that evidence.
            for evaluation in self.out['evaluations']:
                correspondence, _, message = self._correspondence(evaluation)
                if correspondence == 'conflict':
                    self._out_conflict(f"Evaluation {evaluation['index']}: {message}")
        messages, limitations = [], list(self.context.limitations)
        limitations.extend(self.issues)
        limitations.extend(('Live observations never establish scientific acceptance or process ownership',
                            'Evaluations are chronological; accepted optimizer steps are not inferred'))
        if self.duplicate_streams:
            limitations.append('OSZICAR and stdout both declared; OSZICAR exclusively selected to avoid duplicate rows')
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
                else:
                    metrics.extend(self._electronic_metrics(evaluation, rows))
                metrics.extend(self._force_metrics())
                ahead = self.osz_current
                active_ahead = (ahead is not None and ahead['segment'] == self.out_segment
                                and ahead['index'] > evaluation['index'] and bool(ahead['rows']))
                if (ahead is not None and ahead['segment'] == self.out_segment
                        and ahead['index'] > evaluation['index'] and ahead['rows']):
                    provisional = {'index': ahead['index'], 'native': 'pending'}
                    iteration = self._levels(provisional, ahead['rows'][-1]['label'])
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
        return ProgressSnapshot(self.context, 'vasp-live-6.6.1', status=status, phase=phase,
                                calculation=calculation, segment=max(1, self.out_segment),
                                iteration=iteration, metrics=tuple(sorted(metrics, key=lambda item: item.latest.priority)),
                                events=tuple(self.events), completeness=completeness,
                                last_new_evidence_at=self.last_new, messages=tuple(messages),
                                limitations=tuple(dict.fromkeys(limitations)))
