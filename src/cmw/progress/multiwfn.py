"""Bounded Multiwfn native markers, runner events and validator observations.

These evidence kinds remain separate. No menu input, process outcome, log pause,
CPU usage or output-file existence is promoted to scientific validation.
"""
from __future__ import annotations

from collections import deque
import json
from pathlib import Path

from cmw.molecular.multiwfn.runtime import parse_version, resolve_menu_contract
from cmw.molecular.multiwfn.excited_states import THREAD_RE, LOADED_WAVEFUNCTION_RE, SELECTED_STATE_RE
from .models import CardCell, CardGroup, HistoryColumn, HistoryEntry, ProgressEvent, ProgressSnapshot, SettingObservation

PROFILES = {'FMO': 'HOMO/LUMO cubes', 'ESP': 'Density/ESP cubes', 'IGMH': 'Interfragment IGMH', 'IFCT': 'Two-fragment IFCT'}
STAGES = {'prepared': 'Preparation complete', 'launch_requested': 'Analysis launch requested',
          'process_exited': 'Process exited', 'validation_started': 'Output validation started',
          'validation_passed': 'Output validation returned successfully', 'validation_failed': 'Output validation failed'}
MAX_LINES = 512
MAX_QUEUED = 4096


class MultiwfnAdapter:
    def __init__(self, context):
        self.context = context
        self.profile = context.stage or next((s.stage for s in context.sources if s.stage), None)
        self.states = {}
        self.pending = {s.role: deque() for s in context.sources}
        self.generations = {}
        self.events = deque(maxlen=64)
        self.history = deque(maxlen=256)
        self.sequence = 0
        self.segment = 1
        self.issues = deque(maxlen=32)
        self.native_stage = None
        self.runner_stage = None
        self.latest_stage = None
        self.validation = None
        self.version = None
        self.settings = {}
        self.metadata = {}
        self.metadata_source = next((s for s in context.sources if s.role == 'multiwfn_metadata'
            and s.expected_sha256 is not None and s.expected_size is not None), None)
        self.last_new = None
        self.conflict = False
        self.native_conflict = False
        self.unsupported = self.profile not in PROFILES
        self.seen = set()

    def read_roles(self):
        return tuple(role for role, lines in self.pending.items() if not lines)

    @property
    def has_pending_work(self):
        return any(self.pending.values())

    def drain_history(self):
        rows = tuple(self.history)
        self.history.clear()
        return rows

    def _issue(self, message):
        if message not in self.issues:
            self.issues.append(message)

    def _record(self, kind, text, line):
        key = (line.evidence.source_role, line.evidence.generation, kind, text)
        if key in self.seen:
            return
        # An unbounded noisy native session cannot grow the deduplication set.
        if len(self.seen) >= 512:
            self.seen.clear()
        self.seen.add(key)
        self.sequence += 1
        event_id = ':'.join(map(str, (self.context.binding_id, self.context.attempt_id,
            line.evidence.source_role, line.evidence.generation, line.evidence.byte_start, kind)))
        self.events.append(ProgressEvent(event_id, kind, self.profile, self.segment,
            evidence=(line.evidence,), message=text))
        self.history.append(HistoryEntry(event_id, self.sequence, kind=kind,
            native_label=text, state='OBSERVED', coverage='PARTIAL', evidence=(line.evidence,),
            binding_id=self.context.binding_id, attempt_id=self.context.attempt_id,
            stage=self.profile, segment=self.segment,
            annotations=('Native, runner and validator evidence remain independent',)))
        self.last_new = line.observed_at
        if kind in {'native', 'runner'}:
            self.latest_stage = text

    def _metadata(self, line):
        if self.metadata_source is None:
            self._issue('Runner/validator association is unpinned; only native markers are observed')
            return
        try:
            record = json.loads(line.text)
        except ValueError:
            self.conflict = True
            self._issue('Malformed pinned Multiwfn association')
            return
        if (not isinstance(record, dict) or record.get('schema') != 1
                or record.get('profile') != self.profile
                or record.get('attempt_directory') != str(Path(line.evidence.path).parent)
                or not record.get('scientific_attempt_id') or not record.get('source_target_id')
                or not record.get('source_attempt_id')):
            self.conflict = True
            self._issue('Pinned association does not identify the declared scientific attempt/profile')
            return
        self.metadata = record
        for key in ('version', 'threads', 'settings_sha256', 'menu_sha256', 'state_index',
                    'scientific_attempt_id', 'source_target_id', 'source_attempt_id', 'source_result', 'expected_roles'):
            if key in record:
                self.settings[key] = SettingObservation(key, record[key], 'pinned preparation association', 'declared', (line.evidence,))

    def _bound_record(self, line):
        if not self.metadata:
            self._issue('Awaiting pinned attempt/source association; runner and validator claims are not accepted')
            return None
        try:
            record = json.loads(line.text)
        except (json.JSONDecodeError, ValueError):
            self.conflict = True
            self._issue('Malformed complete runner/validator record; partial reader lines remain pending')
            return None
        if (not isinstance(record, dict) or record.get('schema') != 1
                or record.get('profile') != self.profile
                or record.get('attempt_directory') != str(Path(line.evidence.path).parent)
                or not isinstance(record.get('scientific_attempt_id'), str)
                or not record.get('source_target_id') or not record.get('source_attempt_id')):
            self.conflict = True
            self._issue('Runner/validator profile or attempt/source association does not match the declared source')
            return None
        if self.metadata:
            for field in ('scientific_attempt_id', 'source_target_id', 'source_attempt_id', 'source_result'):
                if record.get(field) != self.metadata.get(field):
                    self.conflict = True
                    self._issue('Runner and validator source/attempt identities conflict')
                    return None
        return record

    def _runner(self, line):
        record = self._bound_record(line)
        if record is None:
            return
        kind = record.get('kind')
        if kind not in STAGES:
            self._issue('Unknown runner event kind; observation does not infer its meaning')
            return
        text = STAGES[kind]
        if kind == 'process_exited':
            text += f' ({record.get("value", "unknown")})'
        self.runner_stage = text
        self._record('runner', text, line)

    def _validation_matches_native(self, record):
        source = self.states.get('multiwfn_session')
        version = record.get('native_file_version')
        # ctime includes permission/attribute changes. Reader generations and
        # sentinels still revoke replaced or rewritten native evidence.
        return (source is not None and source.health == 'OK'
                and record.get('native_path') == source.path
                and record.get('native_identity') == list(source.identity or ())
                and isinstance(version, list) and len(version) == 3
                and version[:2] == list((source.file_version or ())[:2]))

    def _validator(self, line):
        record = self._bound_record(line)
        if record is None:
            return
        if (record.get('kind') != 'validator_record' or record.get('status') != 'PASSED'
                or not isinstance(record.get('result_path'), str)
                or not isinstance(record.get('result_sha256'), str)
                or len(record['result_sha256']) != 64):
            self.conflict = True
            self._issue('Unsupported validator projection; no scientific validation is reported')
            return
        if not self._validation_matches_native(record):
            self._issue('Validator record belongs to a different or unavailable native-log version')
            return
        self.validation = record
        self._record('validator', 'Task-specific validator recorded PASSED', line)

    def _native(self, line):
        text = line.text
        version = parse_version(text)
        if version:
            if self.version is not None:
                self.segment += 1
                self.native_stage = None
                self.validation = None
                self.native_conflict = False
                self.unsupported = self.profile not in PROFILES
            try:
                resolve_menu_contract(version)
                if self.profile == 'IFCT' and version != '2026.7.15':
                    raise ValueError('IFCT requires exact 2026.7.15 grammar')
            except ValueError:
                self.unsupported = True
                self._issue(f'Unsupported native Multiwfn version {version}; runner evidence remains separate')
            self.version = version
            expected = self.settings.get('version')
            if expected and expected.value != version:
                self.native_conflict = True
                self._issue('Native version differs from the prepared runtime contract')
            self.settings['native_version'] = SettingObservation('native_version', version, 'native session banner', evidence=(line.evidence,))
            self._record('native', f'Multiwfn {version} banner observed', line)
        if self.unsupported or not self.version:
            return
        threads = THREAD_RE.search(text)
        if threads:
            value = int(threads.group(1))
            self.settings['native_threads'] = SettingObservation('native_threads', value, 'native session', evidence=(line.evidence,))
            expected = self.settings.get('threads')
            if expected and expected.value != value:
                self.native_conflict = True
                self._issue('Native threads differ from the prepared CPU commitment')
        if LOADED_WAVEFUNCTION_RE.fullmatch(text):
            self.native_stage = 'Native source load reported'
            self._record('native', self.native_stage, line)
        selected = SELECTED_STATE_RE.search(text)
        if selected and self.profile == 'IFCT':
            value = int(selected.group(1))
            self.native_stage = f'Native IFCT state {value} selected'
            expected = self.settings.get('state_index')
            if expected and expected.value != value:
                self.native_conflict = True
                self._issue('Native selected state differs from the declared analysis state')
            self._record('native', self.native_stage, line)
        if self.profile == 'IFCT' and text.strip() == 'Construction of interfragment charger-transfer matrix has finished!':
            self.native_stage = 'Native IFCT matrix marker observed'
            self._record('native', self.native_stage, line)

    def update(self, updates, now):
        for update in updates:
            role = update.source.role
            if role not in self.pending:
                continue
            self.states[role] = update.state
            previous = self.generations.get(role)
            self.generations[role] = update.state.generation
            if update.reset or (previous is not None and previous != update.state.generation):
                self.pending[role].clear()
                self.segment += 1
                self.validation = None
                self.latest_stage = None
                if role == 'multiwfn_session':
                    self.native_stage = None
                    self.version = None
                    self.native_conflict = False
                    self.unsupported = self.profile not in PROFILES
                elif role == 'multiwfn_events':
                    self.runner_stage = None
                elif role == 'multiwfn_metadata':
                    self.metadata = {}
                    self.settings.clear()
                self._issue(f'{role} source generation changed; earlier observations are historical')
            if len(self.pending[role]) + len(update.lines) > MAX_QUEUED:
                self.conflict = True
                self._issue('Bounded Multiwfn line queue exceeded; native observation is incomplete')
            self.pending[role].extend(update.lines[:max(0, MAX_QUEUED - len(self.pending[role]))])
        # Read contract events first so native evidence can be compared, but never
        # wait for an event stream to parse an explicitly declared native log.
        remaining = MAX_LINES
        for role in ('multiwfn_metadata', 'multiwfn_events', 'multiwfn_session', 'multiwfn_validation'):
            queue = self.pending.get(role, ())
            while queue and remaining:
                line = queue.popleft()
                remaining -= 1
                if role == 'multiwfn_metadata':
                    self._metadata(line)
                elif role == 'multiwfn_events':
                    self._runner(line)
                elif role == 'multiwfn_validation':
                    self._validator(line)
                else:
                    self._native(line)
        if self.validation and not self._validation_matches_native(self.validation):
            self.validation = None
            self._issue('Native log changed after validation; prior validator evidence is historical')
        conflict = self.conflict or self.native_conflict
        status = 'SOURCE_UNRESOLVED' if conflict else 'UNSUPPORTED' if self.unsupported else 'OK' if self.events else 'PENDING'
        stage = self.latest_stage or self.runner_stage or self.native_stage or 'Awaiting declared evidence'
        expected = self.settings.get('expected_roles')
        output = 'Validator PASSED (recorded)' if self.validation and not conflict else ((str(expected.value) + ' / unvalidated') if expected else 'Not scientifically validated')
        groups = (CardGroup('ANALYSIS', ((CardCell(kind='text', text=PROFILES.get(self.profile, 'Unsupported profile')),),)),
                  CardGroup('STAGE', ((CardCell(kind='text', text=stage),),)),
                  CardGroup('OUTPUT', ((CardCell(kind='text', text=output),),)))
        return ProgressSnapshot(self.context, 'multiwfn-profile-v1', status=status,
            phase=stage, calculation=self.profile, segment=self.segment, events=tuple(self.events),
            completeness='PARTIAL', last_new_evidence_at=self.last_new,
            messages=tuple(self.issues), settings=tuple(self.settings.values()),
            scientific_validation='validator-recorded-passed' if self.validation and not conflict else 'not-assessed',
            history_columns=(HistoryColumn('native_label', 'Evidence'), HistoryColumn('kind', 'Kind')),
            card_groups=groups, limitations=(*self.context.limitations,
                'Runner lifecycle, native markers and final validator records are separate evidence',
                'No progress percentage, ETA or scientific acceptance is inferred from process state',
                'Cube native operation grammar is not qualified beyond version/source-load markers',
                'Validator projection records the established finalizer outcome; observation never finalizes or rehashes products'))
