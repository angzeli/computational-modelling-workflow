"""Bounded Multiwfn native markers, runner events and validator observations.

These evidence kinds remain separate. No menu input, process outcome, log pause,
CPU usage or output-file existence is promoted to scientific validation.
"""
from __future__ import annotations

from collections import deque
from dataclasses import replace
import json
from pathlib import Path

from cmw.molecular.multiwfn.runtime import parse_version, resolve_menu_contract
from cmw.molecular.multiwfn.adapter import RUNNER_EVENT_PREDECESSORS
from cmw.molecular.multiwfn.excited_states import THREAD_RE, LOADED_WAVEFUNCTION_RE, SELECTED_STATE_RE
from .models import HistoryColumn, HistoryEntry, ProgressEvent, ProgressSnapshot, SettingObservation
from .multiwfn_card import PROFILES, multiwfn_card_groups

STAGES = {'prepared': 'Preparation complete', 'launch_requested': 'Analysis launch requested',
          'child_launched': 'Child launched', 'process_exited': 'Process exited', 'validation_started': 'Output validation started',
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
        self.validation = None
        self.validation_line = None
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
        self.runner_sequence = 0
        self.runner_records = {}
        self.pending_validation = None
        self.stream_conflict = False
        self.unavailable_roles = set()

    def read_roles(self):
        return tuple(role for role, lines in self.pending.items() if not lines)

    @property
    def has_pending_work(self):
        return any(self.pending.values()) or self.pending_validation is not None and self._streams_catching_up()

    def drain_history(self):
        rows = tuple(self.history)
        self.history.clear()
        return rows

    def _issue(self, message):
        if message not in self.issues:
            self.issues.append(message)

    def _record(self, kind, text, line, sequence=None):
        key = (line.evidence.source_role, line.evidence.generation, kind, sequence if sequence is not None else text)
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
        if (not isinstance(record, dict) or type(record.get('schema')) is not int
                or record['schema'] not in {1, 2} or record.get('profile') != self.profile
                or record.get('attempt_directory') != str(Path(line.evidence.path).parent)
                or not record.get('scientific_attempt_id') or not record.get('source_target_id')
                or not record.get('source_attempt_id')):
            self.conflict = True
            self._issue('Pinned association does not identify the declared scientific attempt/profile')
            return
        if record['schema'] == 2:
            native = next((source.path for source in self.context.sources if source.role == 'multiwfn_session'), None)
            identity = record.get('events_identity')
            if (not isinstance(record.get('stream_id'), str) or not record['stream_id']
                    or not isinstance(record.get('request_id'), str) or not record['request_id']
                    or type(record.get('stream_generation')) is not int or record['stream_generation'] != 1
                    or not isinstance(identity, list) or len(identity) != 2
                    or any(type(value) is not int or value < 0 for value in identity)
                    or record.get('native_path') != native):
                self.conflict = True
                self._issue('Pinned stream identity or declared native path is invalid')
                return
        else:
            self._issue('Legacy schema-1 association: runner records lack sequenced stream identity')
        self.metadata = record
        keys = ('version', 'threads', 'settings_sha256', 'menu_sha256', 'state_index',
                'spin_manifold', 'fragment_labels', 'fragment_counts', 'fragment_sha256',
                'scientific_attempt_id', 'source_target_id', 'source_attempt_id', 'source_result',
                'expected_roles', 'request_id', 'stream_id', 'stream_generation',
                'source_wavefunction_sha256', 'source_geometry_sha256')
        for key in keys:
            if key in record:
                value = record[key]
                if isinstance(value, list):
                    value = ', '.join(str(item) for item in value)
                if value is None or type(value) in (str, int, float, bool):
                    self.settings[key] = SettingObservation(key, value, 'pinned preparation association', 'declared', (line.evidence,))

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
        fields = ('schema', 'profile', 'attempt_directory', 'scientific_attempt_id',
                  'source_target_id', 'source_attempt_id', 'source_result')
        if self.metadata['schema'] == 2:
            fields = tuple(self.metadata)
        if (not isinstance(record, dict)
                or any(record.get(field) != self.metadata.get(field) for field in fields)
                or record.get('attempt_directory') != str(Path(line.evidence.path).parent)):
            self.conflict = True
            if line.evidence.source_role == 'multiwfn_events':
                self.stream_conflict = True
            self._issue('Runner and validator profile/source/attempt identities conflict with the pinned association')
            return None
        if record['schema'] == 2 and (type(record.get('sequence')) is not int or record['sequence'] < 1):
            self.conflict = True
            self._issue('Runner/validator sequence must be a positive integer')
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
        sequence = None
        if record['schema'] == 2:
            identity = list(line.evidence.file_identity or ())
            if (identity != self.metadata['events_identity']
                    or record.get('event_source_identity') != identity or self.stream_conflict):
                self.stream_conflict = True
                self._issue('Runner stream file identity or reader generation changed; current claims are revoked')
                return
            sequence = record['sequence']
            if sequence <= self.runner_sequence:
                if self.runner_records.get(sequence) == record:
                    self._issue('Duplicate identical runner event ignored; evidence did not advance')
                    return
                self.stream_conflict = True
                self._issue('Runner sequence was replayed with conflicting content or order')
                return
            if sequence != self.runner_sequence + 1:
                self.stream_conflict = True
                self._issue('Runner sequence has a gap or is out of order')
                return
            previous = self.runner_records.get(self.runner_sequence)
            if ((previous is None and kind != 'prepared')
                    or (previous is not None and previous['kind'] not in RUNNER_EVENT_PREDECESSORS[kind])
                    or any(item['kind'] == kind for item in self.runner_records.values())):
                self.stream_conflict = True
                self._issue('Runner lifecycle boundary is out of order or already recorded')
                return
            if kind in {'child_launched', 'process_exited'}:
                value = record.get('value')
                try:
                    parsed = int(value) if type(value) in (str, int) else None
                except ValueError:
                    parsed = None
                if (parsed is None or kind == 'child_launched' and parsed < 1
                        or kind == 'process_exited' and not 0 <= parsed <= 255):
                    self.stream_conflict = True
                    self._issue('Runner child/process value is invalid')
                    return
            self.runner_records[sequence] = record
            self.runner_sequence = sequence
        text = STAGES[kind]
        if kind == 'process_exited':
            text += f' ({record.get("value", "unknown")})'
        if record['schema'] == 1:
            text = 'Legacy: ' + text
        self.runner_stage = text
        self._record('runner', text, line, sequence)

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

    def _streams_catching_up(self):
        return any(self.pending.get(role) or self.states.get(role) is not None and self.states[role].has_more
                   for role in ('multiwfn_events', 'multiwfn_session'))

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
        if record['schema'] == 1:
            self._record('legacy-validator', 'Legacy validator record (unsequenced)', line)
            self._issue('Legacy validator record lacks sequenced stream association; validation is not current')
            return
        required = ('multiwfn_events', 'multiwfn_session', 'multiwfn_validation')
        unavailable = any(self.states.get(role) is None or self.states[role].health not in {'OK', 'MALFORMED'}
                          for role in required)
        if self._streams_catching_up() or unavailable:
            self.pending_validation = line
            return
        source = self.states['multiwfn_validation']
        if source.identity != line.evidence.file_identity or source.generation != line.evidence.generation:
            self._issue('Cached validator projection belongs to an earlier source generation')
            return
        event = self.runner_records.get(record['sequence'])
        if (event is None or event.get('kind') != 'validation_passed' or self.stream_conflict
                or record.get('event_source_identity') != self.metadata['events_identity']):
            self.conflict = True
            self._issue('Validator projection has no matching sequenced validation event')
            return
        if not self._validation_matches_native(record):
            self._issue('Validator record belongs to a different or unavailable native-log version')
            return
        self.validation = record
        self.validation_line = line
        self._record('validator', 'Task-specific validator recorded PASSED', line, record['sequence'])

    def _native(self, line):
        text = line.text
        version = parse_version(text)
        if version:
            if self.version is not None:
                self.segment += 1
                self.native_stage = None
                self.validation = None
                self.native_conflict = False
                for key in ('native_threads', 'native_state', 'native_version'):
                    self.settings.pop(key, None)
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
            self.settings['native_state'] = SettingObservation('native_state', value, 'native session', evidence=(line.evidence,))
            self.native_stage = f'Native IFCT state {value} selected'
            expected = self.settings.get('state_index')
            if expected and expected.value != value:
                self.native_conflict = True
                self._issue('Native selected state differs from the declared analysis state')
            self._record('native', self.native_stage, line)
        if self.profile == 'IFCT' and text.strip() == 'Construction of interfragment charger-transfer matrix has finished!':
            self.native_stage = 'Native IFCT matrix marker observed'
            self._record('native', self.native_stage, line)

    def _reset_role(self, role):
        self.pending[role].clear()
        self.segment += 1
        self.validation = None
        self.validation_line = None
        self.pending_validation = None
        self.events = deque((event for event in self.events
                             if not any(evidence.source_role == role for evidence in event.evidence)), maxlen=64)
        if role == 'multiwfn_session':
            self.native_stage = None
            self.version = None
            self.native_conflict = False
            self.unsupported = self.profile not in PROFILES
            for key in ('native_threads', 'native_state', 'native_version'):
                self.settings.pop(key, None)
        elif role == 'multiwfn_events':
            self.runner_stage = None
            self.stream_conflict = True
            self.runner_records.clear()
            self.runner_sequence = 0
        elif role == 'multiwfn_metadata':
            self.metadata = {}
            self.settings = {key: value for key, value in self.settings.items() if key.startswith('native_')}
            self.runner_stage = None
            self.runner_records.clear()
            self.runner_sequence = 0
            self.events = deque((event for event in self.events if event.kind == 'native'), maxlen=64)
        self._issue(f'{role} source generation changed; earlier observations are historical')

    def update(self, updates, now):
        for update in updates:
            role = update.source.role
            if role not in self.pending:
                continue
            previous_state = self.states.get(role)
            self.states[role] = update.state
            if update.state.health in {'OK', 'MALFORMED'}:
                self.unavailable_roles.discard(role)
            elif previous_state is not None and previous_state.last_success_at is not None:
                self.unavailable_roles.add(role)
                self._issue(f'{role} is unavailable; earlier evidence is cached')
            if role in {'multiwfn_events', 'multiwfn_validation'} and role in self.unavailable_roles:
                self.pending_validation = self.validation_line or self.pending_validation
                self.validation = None
            previous = self.generations.get(role)
            self.generations[role] = update.state.generation
            changed = update.reset or (previous is not None and previous != update.state.generation)
            if changed and update.reason != 'initial':
                self._reset_role(role)
            if role == 'multiwfn_metadata' and update.state.health not in {'OK', 'MALFORMED'}:
                if self.metadata:
                    self._reset_role(role)
                if update.state.health == 'INTEGRITY_ERROR':
                    self.conflict = True
                    self._issue('Pinned preparation association failed integrity verification')
            if len(self.pending[role]) + len(update.lines) > MAX_QUEUED:
                self.conflict = True
                self._issue('Bounded Multiwfn line queue exceeded; native observation is incomplete')
            self.pending[role].extend(update.lines[:max(0, MAX_QUEUED - len(self.pending[role]))])
        # Preparation is parsed before sequenced lifecycle evidence. Native markers
        # remain independent; processing order does not establish cross-file time.
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
        if self.pending_validation and not self._streams_catching_up():
            line, self.pending_validation = self.pending_validation, None
            self._validator(line)
        if self.validation and not self._validation_matches_native(self.validation):
            self.validation = None
            self._issue('Native log changed after validation; prior validator evidence is historical')
        conflict = self.conflict or self.native_conflict or self.stream_conflict
        if conflict:
            self.validation = None
        status = 'SOURCE_CONFLICT' if conflict else 'UNSUPPORTED' if self.unsupported else 'OK' if self.events else 'PENDING'
        native = None if self.native_conflict or 'multiwfn_session' in self.unavailable_roles else self.native_stage
        runner = None if self.stream_conflict or not self.metadata or 'multiwfn_events' in self.unavailable_roles else self.runner_stage
        terminal_runner = runner and any(label in runner for label in ('Process exited', 'Output validation'))
        stage = runner if terminal_runner else native or runner or 'Awaiting declared evidence'
        settings = dict(self.settings)
        for name, value in (('native_stage', native), ('runner_stage', runner)):
            if value:
                settings[name] = SettingObservation(name, value, 'native session' if name == 'native_stage' else 'runner lifecycle', 'observed')
        snapshot = ProgressSnapshot(self.context, 'multiwfn-profile-v2', status=status,
            phase=stage, calculation=self.profile, segment=self.segment, events=tuple(self.events),
            completeness='PARTIAL', read_health='PARTIAL' if self.unavailable_roles else 'PENDING',
            last_new_evidence_at=self.last_new,
            messages=tuple(self.issues), settings=tuple(settings.values()),
            scientific_validation='validator-recorded-passed' if self.validation else 'not-assessed',
            history_columns=(HistoryColumn('native_label', 'Evidence'), HistoryColumn('kind', 'Kind')),
            limitations=(*self.context.limitations,
                'Runner lifecycle, native markers and final validator records are separate evidence',
                'No progress percentage, ETA or scientific acceptance is inferred from process state',
                'Cube native operation grammar is not qualified beyond version/source-load markers',
                'Validator projection records the established finalizer outcome; observation never finalizes or rehashes products'))
        return replace(snapshot, card_groups=multiwfn_card_groups(snapshot))
