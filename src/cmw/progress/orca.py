"""Bounded ORCA 6.1 ordinary DIIS/SOSCF and geometry-table observations.

Only explicitly declared outputs are consumed. Native outcomes remain evidence,
not finalization decisions; no engine, Jobs or filesystem API is used here.
"""
from __future__ import annotations

from collections import deque
from dataclasses import replace
import math
import re

from cmw.molecular.orca.status import (
    NUMBER, VERSION_RE, SCF_CONVERGED_RE, SCF_FAILURE_RE, FINAL_ENERGY_RE,
    RUNTIME_RE, FATAL_PATTERNS, INPUT_ECHO_HEADER_RE, INPUT_ECHO_LINE_RE,
    INPUT_ECHO_END_RE, _number, _keyword_payload,
)
from .models import (
    CardCell, CardGroup, Criterion, HistoryColumn, HistoryEntry, IterationLevel, Metric, MetricSeries,
    ProgressEvent, ProgressSnapshot, Timing, comparable,
)

MAX_LINES = 2048
MAX_PENDING_HISTORY = 4096
CYCLE = re.compile(r'GEOMETRY OPTIMIZATION CYCLE\s+(\d+)', re.I)
GEOMETRY_ROW = re.compile(rf'^\s*(Energy change|RMS gradient|MAX gradient|RMS step|MAX step)\s+({NUMBER})(?:\s+({NUMBER}))?(?:\s+(YES|NO))?\s*$', re.I)
SUMMARY_ROW = re.compile(rf'^\s*Last (Energy change|MAX-Density change|RMS-Density change|DIIS Error|Orbital Gradient|Orbital Rotation)\s+\.+\s+({NUMBER})(?:\s+Tolerance\s*:\s*({NUMBER}))?\s*$', re.I)
DIIS_HEADER = 'Iteration Energy (Eh) Delta-E RMSDP MaxDP DIISErr Damp Time(sec)'
SOSCF_HEADER = 'Iteration Energy (Eh) Delta-E RMSDP MaxDP MaxGrad Time(sec)'
GEOMETRY = {
    'energy change': ('energy_change', 'Geometry |dE|', 'Magnitude of native geometry energy change', 'Eh', 30),
    'rms gradient': ('rms_gradient', 'RMS gradient', 'Native optimization-table RMS gradient in reported optimization coordinate scope', 'Eh/bohr', 20),
    'max gradient': ('max_gradient_component', 'Max gradient component', 'Native optimization-table maximum gradient component; not a per-atom force-vector norm', 'Eh/bohr', 10),
    'rms step': ('rms_step', 'RMS step', 'Native optimization-table RMS displacement in reported optimization coordinate scope', 'bohr', 40),
    'max step': ('max_step', 'Max step', 'Native optimization-table maximum displacement in reported optimization coordinate scope', 'bohr', 50),
}
SCF = {
    'energy': ('SCF energy', 'Native SCF iteration energy', 'Eh', 90, None),
    'energy_change': ('SCF |dE|', 'Magnitude of native SCF iteration energy change', 'Eh', 70, 'TolE'),
    'rms_density_change': ('RMS density change', 'Native RMS density-matrix change; header prints no unit', 'unspecified', 80, 'TolRMSP'),
    'max_density_change': ('Max density change', 'Native maximum density-matrix change; header prints no unit', 'unspecified', 85, 'TolMaxP'),
    'diis_error': ('DIIS error', 'Native DIIS error, not a VASP charge residual; header prints no unit', 'unspecified', 25, 'TolErr'),
    'max_orbital_gradient': ('Max orbital gradient', 'Native SOSCF maximum orbital-gradient component; header prints no unit', 'unspecified', 25, 'TolG'),
}


def _precision(token):
    mantissa, _, exponent = token.lower().replace('d', 'e').partition('e')
    digits = len(mantissa.split('.')[1]) if '.' in mantissa else 0
    try:
        return 10.0 ** (int(exponent or 0) - digits)
    except OverflowError:
        return None


def _finite(token):
    value = _number(token)
    if not math.isfinite(value):
        raise ValueError('nonfinite number')
    return value


def _relation(value, precision, threshold):
    if threshold is None:
        return None
    radius = (precision or 0) / 2
    if value + radius < threshold:
        return 'BELOW'
    if max(0, value - radius) >= threshold:
        return 'AT_OR_ABOVE'
    return 'UNCERTAIN'


class OrcaAdapter:
    """One bounded native stream per declared stage, using shared history upserts."""

    def __init__(self, context):
        self.context = context
        self.sources = tuple(s for s in context.sources
                             if s.role.lower() == 'orca_output' or s.role.lower().startswith('orca_output:'))
        self.states = {}
        self.pending = {s.role: deque() for s in self.sources}
        self.generations = {}
        self.active_source = None
        self.segment = 0
        self.stage = context.stage.upper() if context.stage else None
        self.sequence = 0
        self.history = {}
        self.events = deque(maxlen=64)
        self.issues = deque(maxlen=32)
        self.series = {}
        self.completed = deque(maxlen=2)
        self.observation = (None, True, None)
        self.last_new = None
        self.conflict = False
        self.expected_stage = None
        self.run_segment = 1
        self.run_evidence = None
        self._clear_run()

    def _clear_run(self):
        self.version = None
        self.current = None
        self.algorithm = None
        self.algorithm_epoch = 0
        self.last_iteration = None
        self.table = None
        self.geometry_table = None
        self.in_echo = False
        self.echo_tokens = []
        self.echo_unsupported = None
        self.unsupported = None
        self.scf_mode = None
        self.scf_budget = None
        self.geom_budget = None
        self.tolerances = {}
        self.settings_block = None
        self.normal = False
        self.fatal = False
        self.opt_converged = False
        self.series.clear()
        self.completed.clear()

    def set_observation(self, monotonic_now, *, historical, clock_id):
        self.observation = (monotonic_now, historical, clock_id)

    def read_roles(self):
        if len(self.history) >= MAX_PENDING_HISTORY:
            return ()
        return tuple(s.role for s in self.sources if not self.pending[s.role])

    @property
    def has_pending_work(self):
        return any(self.pending.values())

    def drain_history(self):
        result = tuple(sorted(self.history.values(), key=lambda e: e.sequence))
        self.history.clear()
        return result

    def _issue(self, message):
        if message not in self.issues:
            self.issues.append(message)

    def _id(self, line, kind):
        ref = line.evidence
        return ':'.join(map(str, (self.context.binding_id, self.context.attempt_id,
             ref.source_role, ref.path, ref.generation, ref.file_identity,
             self.segment, ref.byte_start, kind)))

    def _event(self, kind, line, message):
        self.events.append(ProgressEvent(self._id(line, kind), kind, self.stage,
            max(1, self.segment), self.current['context'] if self.current else (),
            (line.evidence,), message))
        self.last_new = line.observed_at

    def _boundary(self, line, stage=None, *, reset=False):
        self._close_current(line)
        previous_version = self.version
        self._clear_run()
        self.segment += 1
        self.stage = stage.upper() if stage else None
        if self.stage not in {'OPT', 'SP', None}:
            self.unsupported = f'{self.stage} progress semantics are unsupported; raw logs and Files remain available'
        if not reset:
            self.version = previous_version
        self._event('segment-start', line, f'Native source/stage boundary: {stage or "unidentified"}')

    def _new_evaluation(self, line, label=None):
        self._close_current(line)
        self.sequence += 1
        kind = 'geometry' if self.stage == 'OPT' and label is not None else 'evaluation'
        label = label or 'single point'
        level = IterationLevel(kind, label, f'{self.stage or "SCF"} cycle {label}' if kind == 'geometry' else f'{self.stage or "SCF"} evaluation', self.geom_budget)
        self.current = {'id': self._id(line, kind), 'sequence': self.sequence,
            'context': (level,), 'kind': kind, 'label': str(label),
            'evidence': line.evidence, 'metrics': {}, 'state': 'PARTIAL',
            'coverage': 'PARTIAL', 'children': 0, 'scf': None, 'last_row': None,
            'start': self.observation, 'end': None, 'annotations': [], 'table_complete': False}
        self.algorithm = self.table = None
        self.algorithm_epoch = 0
        self.last_iteration = None
        self.geometry_table = None
        self.series = {k: v for k, v in self.series.items() if k.startswith('orca.geometry.')}
        self._publish()

    def _timing(self, record):
        start, historical, clock = record['start']
        end_info = record['end'] or self.observation
        end, end_historical, end_clock = end_info
        context = record['context']
        if (not historical and not end_historical and start is not None and end is not None
                and clock == end_clock and end > start):
            return Timing('orca.observer.interval', 'Observed interval', end - start,
                quantity='observation-interval', source='observer', context=context,
                stage=self.stage, segment=self.segment, completeness='PARTIAL',
                start_evidence=(record['evidence'],), clock_id=clock,
                reason='Same-session observation interval; buffered output and unobserved start mean this is not execution duration')
        return Timing('orca.evaluation.duration', 'Evaluation duration', context=context,
            stage=self.stage, segment=self.segment,
            reason='No scoped native wall duration; historical, buffered or reset observer interval unavailable')

    def _publish(self):
        r = self.current
        if not r:
            return
        self.history[r['id']] = HistoryEntry(r['id'], r['sequence'], r['context'],
            kind=r['kind'], native_label=r['label'], state=r['state'], coverage=r['coverage'],
            metrics=tuple(r['metrics'].values()), timings=(self._timing(r),),
            evidence=(r['evidence'],), binding_id=self.context.binding_id,
            attempt_id=self.context.attempt_id, stage=self.stage, segment=self.segment,
            child_count=r['children'], annotations=tuple(r['annotations']))

    def _close_current(self, line):
        if self.current:
            r = self.current
            if r['end'] is None:
                r['end'] = self.observation
            if r['state'] == 'PARTIAL':
                r['state'] = 'INCOMPLETE'
            self._publish()

    def _metric(self, semantic, label, definition, unit, token, line, *, context=None,
                threshold=None, priority=100, native=None, scope=''):
        signed = _finite(token)
        magnitude = semantic.endswith('energy_change')
        value = abs(signed) if magnitude else signed
        precision = _precision(token)
        criterion = None
        if threshold is not None and threshold >= 0:
            criterion = Criterion(threshold, unit, '<', 'native-output-table' if semantic.startswith('orca.geometry.') else 'native-output-tolerance',
                definition='Printed criterion; native outcome and configured convergence-check mode remain separate')
        relation = _relation(value, precision, threshold)
        qualification = 'observed'
        if native:
            qualification = f'native-{native.lower()}; numeric-{(relation or "unavailable").lower()}'
        ctx = context or self.current['context']
        comparison = () if semantic.startswith('orca.geometry.') else self.current['context']
        metric = Metric(semantic, label, definition, unit, value, ctx, self.stage,
            self.segment, precision, token, signed, qualification, (line.evidence,),
            line.observed_at, criterion, relation=relation, priority=priority,
            comparison_context=comparison, comparison_scope=scope)
        previous = self.series.get(semantic)
        self.series[semantic] = MetricSeries(metric, previous.latest if previous and comparable(metric, previous.latest) else None)
        return metric

    def _native_stage(self, line, stage):
        if self.expected_stage is not None and stage in {'OPT', 'SP', 'FREQ'} and stage != self.expected_stage:
            self.conflict = True
            self._issue(f'Declared stage {self.expected_stage} conflicts with initial native {stage} evidence')
            return
        self.expected_stage = None
        if self.stage != stage:
            self._boundary(line, stage)
        if stage not in {'OPT', 'SP', None}:
            self.unsupported = f'{stage} progress semantics are unsupported; raw logs and Files remain available'

    def _read_echo(self, line):
        text = line.text
        if INPUT_ECHO_HEADER_RE.match(text):
            self.in_echo = True
            self.echo_tokens = []
            self.echo_unsupported = None
            return True
        if not self.in_echo:
            return False
        if INPUT_ECHO_END_RE.search(text):
            self.in_echo = False
            tokens = {x.upper() for x in self.echo_tokens}
            special = self.echo_unsupported or next((x for x in tokens if x in {'OPTTS', 'NEB', 'NEB-TS', 'IRC', 'MD', 'CASSCF', 'DELTASCF', 'TDDFT', 'TDA', 'SCAN', 'COMPOUND'}), None)
            if special:
                self._native_stage(line, special)
            elif any(x in tokens for x in ('OPT', 'TIGHTOPT', 'LOOSEOPT', 'VERYTIGHTOPT')):
                self._native_stage(line, 'OPT')
            elif tokens & {'FREQ', 'NUMFREQ', 'ANFREQ'}:
                self._native_stage(line, 'FREQ')
            elif 'SP' in tokens or self.stage is None:
                self._native_stage(line, 'SP')
            return True
        numbered = INPUT_ECHO_LINE_RE.match(text)
        if numbered:
            payload = _keyword_payload(numbered.group(1))
            if payload:
                self.echo_tokens = (self.echo_tokens + payload.split())[:128]
                if any(t.upper().startswith(('NEB', 'OPTTS', 'TDDFT', 'DELTASCF')) for t in payload.split()):
                    self.echo_tokens.append('COMPOUND')
            if re.match(r'\s*(scan|iroot|followiroot|nroots)\b', numbered.group(1), re.I):
                self.echo_tokens.append('SCAN' if 'scan' in numbered.group(1).lower() else 'TDDFT')
            if re.search(r'%\s*(tddft|casscf|md|scan|compound)\b', numbered.group(1), re.I):
                self.echo_tokens.append(re.search(r'%\s*(\w+)', numbered.group(1)).group(1))
            if '$new_job' in numbered.group(1).lower():
                self.echo_tokens.append('COMPOUND')
            if re.search(r'%\s*geom\b.*\bscan\b', numbered.group(1), re.I):
                self.echo_tokens.append('SCAN')
            for token in self.echo_tokens:
                if token.upper() in {'OPTTS', 'NEB', 'NEB-TS', 'IRC', 'MD', 'CASSCF', 'DELTASCF', 'TDDFT', 'TDA', 'SCAN', 'COMPOUND'}:
                    self.echo_unsupported = token.upper()
                    break
            self.echo_tokens = self.echo_tokens[-128:]
        return True

    def _read(self, line):
        text, normalized = line.text, ' '.join(line.text.split())
        match = VERSION_RE.search(text)
        if match:
            if self.version is not None or self.current is not None:
                declared = next((s.stage for s in self.sources if s.role == line.evidence.source_role), None)
                self._boundary(line, declared, reset=True)
                self.expected_stage = None
            self.run_segment = self.segment
            self.run_evidence = line.evidence
            self.version = match.group(1)
            if not self.version.startswith('6.1.'):
                self.unsupported = f'Unestablished ORCA dialect {self.version}'
            return
        if self._read_echo(line):
            return
        if re.search(r'\$new_job|COMPOUND JOB|COMPOUND SCRIPT', text, re.I):
            self._native_stage(line, 'COMPOUND')
        if re.search(r'VIBRATIONAL FREQUENCIES|ORCA (?:NUMERICAL|ANALYTICAL) FREQUENCIES|NUMERICAL FREQUENCY CALCULATION', text, re.I):
            self._native_stage(line, 'FREQ')
        if 'FINAL ENERGY EVALUATION AT THE STATIONARY POINT' in text:
            self._native_stage(line, 'SP')
        if any(p.search(text) for p in FATAL_PATTERNS) or SCF_FAILURE_RE.search(text):
            self.fatal = True
            self._event('native-failure', line, 'Native failure evidence; no process-exit inference')
            if self.current:
                self.current['state'] = 'NATIVE_FAILURE'
                self.current['end'] = self.observation
                self._publish()
            self.table = None
        if 'ORCA TERMINATED NORMALLY' in text:
            self.normal = True
            self._event('normal-termination', line, 'Native normal termination; scientific acceptance not assessed')
            self._close_current(line)
        runtime = RUNTIME_RE.search(text)
        if runtime:
            if not self.version or not self.version.startswith('6.1.') or self.run_evidence is None:
                self._issue('Native run timing unavailable without an established ORCA 6.1 run boundary')
                return
            days, hours, minutes, seconds = runtime.groups()
            milliseconds = re.search(r'(-?\d+)\s+msec\b', text)
            if _finite(seconds) < 0 or (milliseconds and int(milliseconds.group(1)) < 0):
                self._issue('Negative native total runtime ignored')
                return
            value = int(days) * 86400 + int(hours) * 3600 + int(minutes) * 60 + _finite(seconds)
            if milliseconds:
                value += int(milliseconds.group(1)) / 1000
            if value >= 0:
                self.sequence += 1
                entry_id = self._id(line, 'run-timing')
                run_context = (IterationLevel('run', self.run_segment, 'Entire native ORCA run'),)
                timing = Timing('orca.run.wall', 'Total run time', value, quantity='cumulative',
                    source='native', context=run_context, stage=None, segment=self.run_segment,
                    completeness='COMPLETE', evidence=(self.run_evidence, line.evidence), precision=0.001 if milliseconds else _precision(seconds),
                    reason='Entire native ORCA run including all stages; not a final-stage or per-cycle time and never differenced')
                self.history[entry_id] = HistoryEntry(entry_id, self.sequence, run_context, kind='run-timing',
                    native_label='Total run time', state='OBSERVED', coverage='COMPLETE',
                    timings=(timing,), evidence=(line.evidence,), binding_id=self.context.binding_id,
                    attempt_id=self.context.attempt_id, stage=None, segment=self.run_segment)
            return
        if self.unsupported or not self.version or not self.version.startswith('6.1.'):
            return
        cycle = CYCLE.search(text)
        if cycle:
            previous_label = self.current['context'][0].index if self.current else None
            if self.opt_converged or (isinstance(previous_label, int) and int(cycle.group(1)) <= previous_label):
                self._boundary(line, 'OPT')
                self._issue('Geometry cycle label reset or post-convergence cycle begins a separate segment')
            self._native_stage(line, 'OPT')
            self._new_evaluation(line, int(cycle.group(1)))
            self._event('geometry-cycle', line, 'Native optimization cycle; accepted/trial status not inferred')
            return
        if re.search(r'THE OPTIMIZATION HAS CONVERGED', text, re.I):
            self.opt_converged = True
            self._event('optimization-converged', line, 'Native optimization convergence marker; no synthesized criterion AND')
        if 'Convergence Tolerances:' in text:
            self.settings_block = 'geometry'
        elif 'Convergence Tolerance:' in text or 'SCF Procedure:' in text:
            self.settings_block = 'scf'
        budget = re.search(r'(Maximum # iterations|Max\. no of cycles)\s+MaxIter\s+\.+\s+(\d+)', text)
        if budget:
            if budget.group(1).startswith('Maximum'):
                self.scf_budget = int(budget.group(2))
            else:
                self.geom_budget = int(budget.group(2))
        mode = re.search(r'ConvCheckMode\s+\.+\s+(.+?)\s*$', text)
        if mode:
            self.scf_mode = mode.group(1)
        setting = re.search(rf'\b(TolE|TolRMSP|TolMaxP|TolErr|TolG|TolX)\s+\.+\s+({NUMBER})', text, re.I)
        if setting and self.settings_block == 'scf':
            key = next(k for k in ('TolE', 'TolRMSP', 'TolMaxP', 'TolErr', 'TolG', 'TolX') if k.lower() == setting.group(1).lower())
            self.tolerances[key] = _finite(setting.group(2))
        if normalized in (DIIS_HEADER, SOSCF_HEADER):
            if self.normal:
                self._boundary(line, self.stage)
            if not self.current:
                self._new_evaluation(line)
            self.algorithm = 'DIIS' if normalized == DIIS_HEADER else 'SOSCF'
            self.algorithm_epoch += 1
            self.table = self.algorithm
            self.last_iteration = None
            self.series = {k: v for k, v in self.series.items() if not k.startswith('orca.scf.')}
            self._event('scf-algorithm', line, self.algorithm + ' native iteration header')
            return
        if 'Iteration' in text and 'Energy' in text and ('TRAH' in text or 'RMSDP' in text):
            self.table = None
            self._issue('Unrecognized SCF iteration header; no positional fallback')
        if ('TRAH-SCF' in text or 'T-R-A-H' in text) and 'CNVTRAH' not in text:
            self.table = None
            self.algorithm = 'TRAH (unsupported)'
            self.series = {k: v for k, v in self.series.items() if not k.startswith('orca.scf.')}
            self._issue('TRAH iteration grammar is not implemented')
        if self.table and re.match(r'^\s*\d+\s', text):
            self._scf_row(line)
            return
        converged = SCF_CONVERGED_RE.search(text)
        if converged:
            if not self.current:
                self._new_evaluation(line)
            self.current['scf'] = True
            self.current['annotations'] = (self.current['annotations'] + [f'Native SCF converged after {converged.group(1)} cycles; summary is not live iteration evidence'])[-32:]
            self._event('scf-converged', line, f'Native SCF convergence after {converged.group(1)} cycles')
            self.table = None
            self._publish()
        energy = FINAL_ENERGY_RE.match(text)
        if energy:
            if not self.current:
                self._new_evaluation(line)
            metric = self._metric('orca.final_energy', 'Final energy', 'Native FINAL SINGLE POINT ENERGY observation', 'Eh', energy.group(1), line, priority=100)
            self.current['metrics'][metric.semantic_id] = metric
            if self.stage != 'OPT' and self.current['state'] != 'NATIVE_FAILURE':
                self.current['state'] = 'OBSERVED'
                self.current['coverage'] = 'COMPLETE'
                self.current['end'] = self.observation
            self._publish()
        summary = SUMMARY_ROW.match(text)
        if summary and self.current:
            self._summary(summary, line)
        if '|Geometry convergence|' in text and self.current and self.stage == 'OPT':
            self.geometry_table = {}
            self.table = None
            return
        if self.geometry_table is not None:
            row = GEOMETRY_ROW.match(text)
            if row:
                key = row.group(1).lower()
                self.geometry_table[key] = (row, line)
                return
            if self.geometry_table and re.match(r'^\s*-{5,}\s*$', text):
                self._finish_geometry()
                return
            if re.match(r'^\s*(Energy change|RMS gradient|MAX gradient|RMS step|MAX step)\b', text, re.I):
                self._issue('Malformed geometry row; table remains incomplete')
                self.geometry_table['malformed'] = None

    def _scf_row(self, line):
        tokens = line.text.split()
        expected = 8 if self.table == 'DIIS' else 7
        if len(tokens) != expected or not all(re.fullmatch(NUMBER, x) for x in tokens[1:]):
            self._issue('Malformed/partial SCF iteration row ignored')
            return
        try:
            values = [_finite(x) for x in tokens[1:]]
        except (ValueError, OverflowError):
            self._issue('Nonfinite SCF row ignored')
            return
        if any(x < 0 for x in values[2:]):
            self._issue('Negative SCF norm/time row ignored')
            return
        index = int(tokens[0])
        if self.last_iteration is not None and index <= self.last_iteration:
            self.algorithm_epoch += 1
            self.series = {k: v for k, v in self.series.items() if not k.startswith('orca.scf.')}
            self._event('scf-reset', line, 'Native iteration label reset; separate comparison context')
        self.last_iteration = index
        r = self.current
        context = (*r['context'], IterationLevel('algorithm', self.algorithm_epoch, self.algorithm),
                   IterationLevel('scf', index, f'SCF {index}', self.scf_budget))
        metric_names = ('energy', 'energy_change', 'rms_density_change', 'max_density_change',
                        'diis_error' if self.table == 'DIIS' else 'max_orbital_gradient')
        metrics = []
        for name, token in zip(metric_names, tokens[1:6]):
            label, definition, unit, priority, tol = SCF[name]
            metric = self._metric('orca.scf.' + name, label, definition, unit, token, line,
                context=context, threshold=self.tolerances.get(tol), priority=priority,
                scope=f'{self.algorithm}:{self.algorithm_epoch}:mode={self.scf_mode or "unknown"}')
            metrics.append(metric)
        self.sequence += 1
        row_id = self._id(line, 'scf')
        timing = Timing('orca.scf.duration', 'SCF duration', context=context, stage=self.stage,
            segment=self.segment, reason=f'Native Time(sec)={tokens[-1]} has no established CPU/wall clock semantics; not interpreted')
        entry = HistoryEntry(row_id, self.sequence, context, parent_id=r['id'], kind='scf',
            native_label=str(index), state='OBSERVED', coverage='COMPLETE', metrics=tuple(metrics),
            timings=(timing,), evidence=(line.evidence,), binding_id=self.context.binding_id,
            attempt_id=self.context.attempt_id, stage=self.stage, segment=self.segment,
            annotations=(f'{self.algorithm}; native Time(sec)={tokens[-1]} preserved as diagnostic only',))
        self.history[row_id] = entry
        r['children'] += 1
        r['last_row'] = entry
        self.last_new = line.observed_at
        self._publish()

    def _summary(self, match, line):
        key, token, tolerance = match.groups()
        key = key.lower()
        names = {'energy change': ('energy_change', 'Eh'), 'max-density change': ('max_density_change', 'unspecified'),
                 'rms-density change': ('rms_density_change', 'unspecified'), 'diis error': ('diis_error', 'unspecified'),
                 'orbital gradient': ('orbital_gradient', 'unspecified'), 'orbital rotation': ('orbital_rotation', 'unspecified')}
        name, unit = names[key]
        metric = self._metric('orca.scf_summary.' + name, 'Summary ' + key,
            'Native post-SCF summary; only energy has an established unit; not assigned to an iteration or substituted for a live residual',
            unit, token, line, threshold=_finite(tolerance) if tolerance else None,
            priority=150, scope='post-scf-summary')
        self.current['metrics'][metric.semantic_id] = metric
        self._publish()

    def _finish_geometry(self):
        table = self.geometry_table
        self.geometry_table = None
        required = {'rms gradient', 'max gradient', 'rms step', 'max step'}
        complete = required.issubset(table) and 'malformed' not in table
        for key, item in table.items():
            if item is None:
                continue
            row, line = item
            suffix, label, definition, unit, priority = GEOMETRY[key]
            token, threshold_token, native = row.group(2, 3, 4)
            threshold = _finite(threshold_token) if threshold_token else None
            if key != 'energy change' and _finite(token) < 0:
                complete = False
                self._issue('Negative geometry norm ignored')
                continue
            metric = self._metric('orca.geometry.' + suffix, label, definition, unit,
                token, line, threshold=threshold, priority=priority, native=native,
                scope='native-optimization-table; constraints not independently reconstructed')
            self.current['metrics'][metric.semantic_id] = metric
        self.current['table_complete'] = complete
        self.current['coverage'] = 'COMPLETE' if complete else 'PARTIAL'
        if self.current['state'] != 'NATIVE_FAILURE':
            self.current['state'] = 'OBSERVED' if complete else 'PARTIAL'
        if complete:
            self.current['end'] = self.observation
            self.completed.append(self._timing(self.current))
        self.current['annotations'] = (self.current['annotations'] + ['Native geometry table scope; no independent constrained-force reconstruction or accepted-step inference'])[-32:]
        self._publish()

    @staticmethod
    def _columns():
        return (HistoryColumn('context', 'CONTEXT', kind='context'), HistoryColumn('state', 'STATE'),
            HistoryColumn('maxg', 'MAX GRAD', 'orca.geometry.max_gradient_component', kinds=('geometry',)),
            HistoryColumn('rmsg', 'RMS GRAD', 'orca.geometry.rms_gradient', kinds=('geometry',)),
            HistoryColumn('de', '|dE|', 'orca.scf.energy_change', kinds=('scf',)),
            HistoryColumn('diis', 'DIIS ERR', 'orca.scf.diis_error', kinds=('scf',)),
            HistoryColumn('soscf', 'ORB GRAD', 'orca.scf.max_orbital_gradient', kinds=('scf',)),
            HistoryColumn('duration', 'TIMING', kind='timing'))

    def _card_groups(self):
        labels = (('geometry', 'G'), ('evaluation', 'E'), ('scf', 'SCF'))
        geometry = {'context_kinds': ('geometry', 'evaluation'), 'context_labels': labels}
        scf = {'context_kinds': ('scf',), 'context_labels': labels}
        gradient = ('orca.geometry.max_gradient_component',)
        step = ('orca.geometry.max_step',)
        diagnostic = ('orca.scf.max_orbital_gradient',) if self.algorithm == 'SOSCF' else ('orca.scf.diis_error',)
        return (
            CardGroup('SCF', ((CardCell(kind='context', context_kinds=('geometry', 'evaluation')),
                               CardCell(kind='context', context_kinds=('scf',), text='SCF unavailable')),)),
            CardGroup('GRADIENT', ((CardCell(gradient, 'Max comp', text='— (not observed)', **geometry),
                                    CardCell(gradient, kind='target')),)),
            CardGroup('DISPLACE', ((CardCell(step, 'Max step', text='— (not observed)', **geometry),
                                    CardCell(step, kind='target')),)),
            CardGroup('ELECTRONIC', ((CardCell(('orca.scf.energy_change',), '|dE|', **scf),
                                      CardCell(diagnostic, 'Orb grad' if self.algorithm == 'SOSCF' else 'DIIS', **scf)),)),
        )

    def update(self, updates, now):
        for update in updates:
            role = update.source.role
            if role not in self.pending:
                continue
            previous = self.generations.get(role)
            self.generations[role] = update.state.generation
            self.states[role] = update.state
            if update.reset and previous is not None and previous > 0:
                for queue in self.pending.values():
                    queue.clear()
                if len(self.sources) > 1:
                    self.conflict = True
                    self._issue('Multi-source replacement requires a fresh explicit binding; no cross-generation history splice')
                self.history.clear()
                self.events.clear()
                self._clear_run()
                self.segment = 0
                self.active_source = None
                self._issue('Declared source generation changed; old observations invalidated')
            self.pending[role].extend(update.lines)
        count = 0
        for source in self.sources:
            queue = self.pending[source.role]
            while queue and count < MAX_LINES and len(self.history) < MAX_PENDING_HISTORY:
                line = queue.popleft()
                count += 1
                if self.conflict:
                    continue
                if self.active_source != source.role:
                    old_index = next((i for i, s in enumerate(self.sources) if s.role == self.active_source), -1)
                    new_index = self.sources.index(source)
                    if old_index > new_index:
                        self.conflict = True
                        self._issue('Earlier declared stage grew after a later stage began; association unresolved')
                        continue
                    self.active_source = source.role
                    self._boundary(line, source.stage or self.context.stage, reset=True)
                    self.expected_stage = (source.stage or self.context.stage or '').upper() or None
                try:
                    self._read(line)
                except (ValueError, OverflowError):
                    self._issue('Malformed or nonfinite native numeric evidence ignored')
                    if self.geometry_table is not None:
                        self.geometry_table['malformed'] = None
            state = self.states.get(source.role)
            if queue or (state and state.has_more):
                break
        self._publish()
        status = 'OBSERVED' if self.current else 'PENDING'
        messages = []
        if self.scf_mode:
            messages.append(f'Native SCF convergence-check mode: {self.scf_mode}; no synthetic global AND')
            if self.scf_mode not in {'0', '1', '2', 'Total+1el-Energy', 'All', 'One'}:
                messages.append('Unrecognized convergence-check mode; native tolerance values are diagnostic')
        else:
            messages.append('SCF convergence-check mode unavailable; printed tolerances do not establish a stopping rule')
        if self.current and self.stage == 'OPT' and not self.current['table_complete']:
            messages.append('Current cycle geometry table unavailable/incomplete; carried values retain their earlier cycle labels')
        if self.opt_converged:
            messages.append('Native optimization convergence marker observed; scientific validation remains separate')
        if self.fatal:
            messages.append('Native failure evidence observed')
        if not self.version:
            messages.append('Awaiting supported ORCA version and grammar evidence')
        metrics = tuple(sorted(self.series.values(), key=lambda s: s.latest.priority))
        if self.unsupported:
            status = 'UNSUPPORTED'
            messages.append(self.unsupported)
            metrics = ()
        if self.conflict:
            status = 'SOURCE_UNRESOLVED'
            metrics = ()
        phase = f'{self.stage or "Unidentified stage"} / {self.algorithm or "native evidence"}'
        if self.fatal:
            phase = f'{self.stage or "ORCA"} / native failure'
        iteration = self.current['last_row'].context if self.current and self.current['last_row'] else self.current['context'] if self.current else ()
        limitations = (*self.context.limitations, *self.issues,
            'Read-only native observations; no scientific acceptance or process ownership',
            'Geometry gradients are native components, not atomic force-vector norms; constraint scope is not reconstructed',
            'Time(sec) clock semantics unavailable; total run time is cumulative, not per-cycle')
        return ProgressSnapshot(self.context, 'orca-live-6.1', status=status, phase=phase,
            calculation=self.stage, segment=max(1, self.segment), iteration=iteration,
            metrics=metrics, events=tuple(self.events), completeness='PARTIAL',
            last_new_evidence_at=self.last_new, messages=tuple(messages), limitations=limitations,
            card_groups=self._card_groups(), history_columns=self._columns(), completed_durations=tuple(t for t in reversed(self.completed) if t.value is not None))
