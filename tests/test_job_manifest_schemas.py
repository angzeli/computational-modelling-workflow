"""Phase 0 data-contract validation; never imports or executes a CMW runtime."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests' / 'fixtures' / 'manifests'
KINDS = ('job', 'state', 'annotations')
SCHEMA_KINDS = (*KINDS, 'project', 'legacy-alias', 'integrity')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def kind_for(path):
    if path.name == '.cmw-project.json':
        return 'project'
    return path.name.removesuffix('.json').split('.')[-1]


class JobManifestSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schemas = {kind: read_json(ROOT / 'schemas' / f'{kind}.v1.json') for kind in SCHEMA_KINDS}
        registry = Registry().with_resources(
            (schema['$id'], Resource.from_contents(schema)) for schema in cls.schemas.values()
        )
        cls.formats = FormatChecker()
        if 'date-time' not in cls.formats.checkers:
            raise RuntimeError('Schema tests require jsonschema[format] for date-time assertions')
        cls.validators = {
            kind: Draft202012Validator(schema, registry=registry, format_checker=cls.formats)
            for kind, schema in cls.schemas.items()
        }
        cls.native = {kind: read_json(FIXTURES / 'native' / '.cmw-manifest' / f'{kind}.json')
                      for kind in KINDS}
        cls.native['legacy-alias'] = read_json(FIXTURES / 'journal' / 'example.legacy-alias.json')
        cls.native['integrity'] = read_json(FIXTURES / 'journal' / 'manifest.integrity.json')
        cls.project = read_json(FIXTURES / 'project' / '.cmw-project.json')

    def test_labels_and_alias_targets(self):
        doc = deepcopy(self.native['annotations'])
        doc['events'][0]['label'] = 'Human name / never a path'
        self.validators['annotations'].validate(doc)
        doc['events'][0]['label'] = 42
        self.assertFalse(self.validators['annotations'].is_valid(doc))
        alias = read_json(FIXTURES / 'journal' / 'example.legacy-alias.json')
        for target in ('J10.7', 'node-a:J16.1', 'human label', 'J28'):
            alias['payload']['target'] = target
            self.assertFalse(self.validators['legacy-alias'].is_valid(alias))
        alias['payload']['target'] = 'node-a/research:J16.1'
        self.validators['legacy-alias'].validate(alias)
        del alias['schema_version']
        self.assertFalse(self.validators['legacy-alias'].is_valid(alias))

    def test_annotation_label_fold_contract(self):
        # Phase 0 contract model, not an implementation of the annotation writer.
        value = deepcopy(self.native['annotations'])
        first = value['events'][0]
        first['label'] = 'Checkpoint candidate'
        second = deepcopy(first)
        second.update(sequence=2, label=None, purpose=None, system=None,
                      note=None, outcome='checkpoint-only')
        third = deepcopy(second)
        third.update(sequence=3, label='', outcome=None)
        value.update(events=[first, second, third], revision=3)
        self.validators['annotations'].validate(value)
        labels = []
        current = None
        for event in value['events']:
            if event['label'] is not None:
                current = event['label']
            labels.append(current)
        self.assertEqual(labels, ['Checkpoint candidate', 'Checkpoint candidate', ''])

    def test_engine_neutral_restart_inputs(self):
        value = read_json(FIXTURES / 'native' / 'orca-parent.job.json')
        for engine in ('ORCA', 'Multiwfn'):
            value['engine'] = engine
            self.validators['job'].validate(value)
        for restart in ({}, {'ORCA_GBW': None}, {'ORCA_GBW': 'bad'}, {'../gbw': 'a' * 64}):
            value['restart_inputs'] = restart
            self.assertFalse(self.validators['job'].is_valid(value))

    def test_native_ids_are_positive_unpadded(self):
        for identifier in ('J0', 'J00', 'J01'):
            for kind in KINDS:
                value = deepcopy(self.native[kind])
                value['id'] = identifier
                self.assertFalse(self.validators[kind].is_valid(value))
            value = deepcopy(self.native['job'])
            value['restart_inputs'] = {'WAVECAR': 'a' * 64}
            for parent in (identifier, 'other:' + identifier):
                value['parent'] = parent
                self.assertFalse(self.validators['job'].is_valid(value))

    def test_native_node_fields_require_uuids(self):
        paths = {'job': [('authoring_node',), ('target_node',), ('id_owner_node',)],
                 'state': [('writer_node',), ('binding', 'node_id')],
                 'annotations': [('events', 0, 'author_node')]}
        for kind, fields in paths.items():
            for path in fields:
                for invalid in ('node-a', 'A' * 32, '1' * 31, None):
                    value = deepcopy(self.native[kind])
                    target = value
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = invalid
                    with self.subTest(kind=kind, path=path, invalid=invalid):
                        self.assertFalse(self.validators[kind].is_valid(value))

    def test_environment_prefix_allowlist_and_native_potcar(self):
        value = deepcopy(self.native['job'])
        for name in ('OMP_PROC_BIND', 'OPENBLAS_VERBOSE', 'MKL_DYNAMIC',
                     'VECLIB_MAXIMUM_THREADS', 'GFORTRAN_UNBUFFERED_ALL',
                     'OMPI_MCA_btl'):
            value['launch']['environment'] = {name: '1'}
            self.validators['job'].validate(value)
        for name in ('PATH', 'AWS_SECRET_ACCESS_KEY', 'MY_OMP_THREADS', 'OMP_'):
            value['launch']['environment'] = {name: '1'}
            self.assertFalse(self.validators['job'].is_valid(value))
        value['launch']['environment'] = {}
        for potcar in (None, []):
            value['potcar'] = potcar
            self.assertFalse(self.validators['job'].is_valid(value))

    def test_journal_envelope_and_payloads(self):
        paths = list((FIXTURES / 'journal').glob('*.json'))
        kinds = set()
        for path in paths:
            event = read_json(path)
            kinds.add(event['event'])
            self.validators['integrity'].validate(event)
            for key in ('schema_version', 'sequence', 'request_uuid', 'event', 'timestamp', 'payload'):
                invalid = deepcopy(event)
                del invalid[key]
                self.assertFalse(self.validators['integrity'].is_valid(invalid))
            for key, bad in (('sequence', 0), ('request_uuid', 'alias'),
                             ('event', 'unknown'), ('timestamp', None),
                             ('payload', {})):
                invalid = deepcopy(event)
                invalid[key] = bad
                self.assertFalse(self.validators['integrity'].is_valid(invalid))
            invalid = deepcopy(event)
            invalid['payload']['extra'] = True
            self.assertFalse(self.validators['integrity'].is_valid(invalid))
        self.assertEqual(kinds, {'manifest', 'registration', 'queue_binding',
                                'queue_locator', 'legacy_alias'})
        events = sorted((read_json(path) for path in paths), key=lambda event: event['sequence'])
        self.assertEqual([event['sequence'] for event in events], list(range(1, 6)))
        self.assertEqual(len({event['request_uuid'] for event in events}), 5)
        self.assertLess(events[1]['payload']['binding_sequence'], events[1]['sequence'])
        value = read_json(FIXTURES / 'journal' / 'registration.integrity.json')
        value['payload']['binding'] = None
        self.assertFalse(self.validators['integrity'].is_valid(value))

    def test_volume_identity_survives_device_number_change_contract(self):
        before = self.project['legacy_queues']['node-a']['research']
        after = deepcopy(before)
        for key in ('root_identity', 'database_identity'):
            after[key][0] += 100
            self.assertEqual((before['volume_uuid'], before[key][1]),
                             (after['volume_uuid'], after[key][1]))
        for invalid in (None, 'disk4', '1' * 32):
            value = deepcopy(self.project)
            value['legacy_queues']['node-a']['research']['volume_uuid'] = invalid
            self.assertFalse(self.validators['project'].is_valid(value))
        value = deepcopy(self.project)
        del value['legacy_queues']['node-a']['research']['volume_uuid']
        self.assertFalse(self.validators['project'].is_valid(value))

    def test_schemas_are_draft_2020_12(self):
        for kind, schema in self.schemas.items():
            with self.subTest(kind=kind):
                self.assertEqual(schema['$schema'], 'https://json-schema.org/draft/2020-12/schema')
                Draft202012Validator.check_schema(schema)

    def test_all_positive_documents(self):
        paths = [path for path in FIXTURES.rglob('*.json')
                 if 'invalid' not in path.parts and 'parser-contracts' not in path.parts]
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path.relative_to(FIXTURES)):
                self.validators[kind_for(path)].validate(read_json(path))

    def test_parser_contract_expected_states_only(self):
        paths = sorted((FIXTURES / 'parser-contracts').glob('*.json'))
        outcomes = set()
        for path in paths:
            with self.subTest(path=path.name):
                contract = read_json(path)
                self.assertIsInstance(contract['invocation_bound'], bool)
                self.assertTrue(contract['OUTCAR_lines'] or contract['stdout_lines'])
                self.validators['state'].validate(contract['expected'])
                self.assertEqual(contract['expected']['native_status'],
                                 contract['queue_context']['native_status'])
                self.assertEqual(contract['expected']['binding'], contract['queue_context']['binding'])
                outcomes.add(contract['expected']['scf_result'])
        self.assertEqual(outcomes, {'converged', 'labort', 'nelm_reached', 'crashed', 'unknown'})

    def test_electronic_prefix_contract_coverage(self):
        prefixes = set()
        for path in (FIXTURES / 'parser-contracts').glob('*.json'):
            contract = read_json(path)
            for line in contract['stdout_lines']:
                prefixes.add(line.split()[0].rstrip(':'))
        self.assertTrue({'DAV', 'RMM', 'DMP', 'CG', 'SDA', 'CGA', 'DIA', 'EIG', 'NONE'} <= prefixes)
        for name in ('eig-postprocessing', 'none-postprocessing'):
            contract = read_json(FIXTURES / 'parser-contracts' / (name + '.json'))
            self.assertEqual(contract['expected']['scf_result'], 'unknown')

    def test_negative_documents_fail_for_the_intended_field(self):
        expected_fields = {'bad-id.job.json': 'id', 'unknown-outcome.annotations.json': 'outcome',
                           'missing-version.state.json': 'schema_version'}
        paths = sorted((FIXTURES / 'invalid').glob('*.json'))
        self.assertEqual({path.name for path in paths}, set(expected_fields))
        for path in paths:
            with self.subTest(path=path.name):
                errors = list(self.validators[kind_for(path)].iter_errors(read_json(path)))
                self.assertTrue(errors)
                self.assertTrue(any(expected_fields[path.name] in str(error.path)
                                    or expected_fields[path.name] in error.message for error in errors))

    def test_legacy_ids_require_backfill_on_every_file(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                value = deepcopy(self.native[kind])
                value['id'] = 'node-a:J27.1'
                with self.assertRaises(ValidationError):
                    self.validators[kind].validate(value)
                value['provenance'] = 'backfilled'
                self.validators[kind].validate(value)
                value['id'] = 'node-a/research:J27.1'
                self.validators[kind].validate(value)
                value['provenance'] = 'native'
                with self.assertRaises(ValidationError):
                    self.validators[kind].validate(value)
                value['provenance'] = 'backfilled'
                value['id'] = 'J28'
                self.validators[kind].validate(value)
                for invalid in ('J27.1', '27', 'J-1', 'J1.2.3', 'other:J1', '../J1',
                                'project:node-a:J27.1', 'node/:J27.1',
                                'node//queue:J27.1', 'node/queue/extra:J27.1', 'node/queue:J28'):
                    value['id'] = invalid
                    with self.assertRaises(ValidationError):
                        self.validators[kind].validate(value)

    def test_version_and_closed_fields_on_every_file(self):
        for kind in SCHEMA_KINDS:
            for mutation in ('missing', 'wrong', 'unknown-field'):
                with self.subTest(kind=kind, mutation=mutation):
                    value = deepcopy(self.project if kind == 'project' else self.native[kind])
                    if mutation == 'missing':
                        del value['schema_version']
                    elif mutation == 'wrong':
                        value['schema_version'] = 2
                    else:
                        value['unrecognised'] = True
                    with self.assertRaises(ValidationError):
                        self.validators[kind].validate(value)

    def test_utc_timestamps_are_validated(self):
        for timestamp in ('2026-11-09T12:00:00+00:00', '2026-11-09T12:00:00',
                          '2026-02-30T12:00:00Z', 'not-a-date'):
            with self.subTest(timestamp=timestamp):
                value = deepcopy(self.native['job'])
                value['created_at'] = timestamp
                with self.assertRaises(ValidationError):
                    self.validators['job'].validate(value)
        value['created_at'] = '2026-11-09T12:00:00.123Z'
        self.validators['job'].validate(value)

    def test_hashes_and_native_creation_requirements(self):
        for digest in ('placeholder', '0' * 63, 'A' * 64, None):
            with self.subTest(digest=digest):
                value = deepcopy(self.native['job'])
                value['inputs']['INCAR'] = digest
                with self.assertRaises(ValidationError):
                    self.validators['job'].validate(value)
        for field in ('created_at', 'authoring_node', 'target_node', 'id_owner_node'):
            with self.subTest(field=field):
                value = deepcopy(self.native['job'])
                value[field] = None
                with self.assertRaises(ValidationError):
                    self.validators['job'].validate(value)

    def test_parent_references_and_restart_hash_requirement(self):
        value = deepcopy(self.native['job'])
        for parent in ('node-a:J10.7', 'another-project:node-a:J10.7',
                       'node-a/research:J10.7', 'another-project:node-a/research:J10.7',
                       'J2', 'another-project:J2'):
            with self.subTest(parent=parent):
                value['parent'] = parent
                value['restart_inputs']['WAVECAR'] = None
                with self.assertRaises(ValidationError):
                    self.validators['job'].validate(value)
                value['restart_inputs']['WAVECAR'] = 'a' * 64
                self.validators['job'].validate(value)
        for parent in ('J10.7', '../J10.7', '/fixture-root/J10.7',
                       'too:many:parts:J10.7', 'project:node:J28',
                       'node//queue:J10.7', 'project:node/queue/extra:J10.7'):
            value['parent'] = parent
            with self.assertRaises(ValidationError):
                self.validators['job'].validate(value)
        value['parent'] = None
        value['parent_inferred'] = True
        with self.assertRaises(ValidationError):
            self.validators['job'].validate(value)

    def test_legacy_node_qualifiers_disambiguate_equal_queue_ids(self):
        identifiers = ('node-a:J4.1', 'node-b:J4.1')
        self.assertEqual(len(set(identifiers)), 2)
        for identifier in identifiers:
            for kind in KINDS:
                with self.subTest(identifier=identifier, kind=kind):
                    value = deepcopy(self.native[kind])
                    value.update(id=identifier, provenance='backfilled')
                    self.validators[kind].validate(value)
            parent = deepcopy(self.native['job'])
            parent['parent'] = identifier
            parent['restart_inputs']['WAVECAR'] = 'a' * 64
            self.validators['job'].validate(parent)
        node_tokens = [identifier.split(':')[0] for identifier in identifiers]
        self.assertEqual(len({self.project['legacy_nodes'][token] for token in node_tokens}), 2)

    def test_legacy_queue_reference_selection_contract(self):
        # A synthetic proposed lookup index, not the runtime resolver or allocator.
        index = {
            'node-a:J4.1': ('node-a/research:J4.1', 'node-a/secondary:J4.1'),
            'node-a:J5.1': ('node-a/research:J5.1',),
        }
        expected_short_allowed = {'node-a:J4.1': False, 'node-a:J5.1': True}
        for short, candidates in index.items():
            with self.subTest(reference=short):
                self.assertEqual(expected_short_allowed[short], len(candidates) == 1)
                self.assertEqual(len(candidates), len(set(candidates)))
                for reference in (short, *candidates):
                    value = deepcopy(self.native['job'])
                    value.update(id=reference, provenance='backfilled')
                    self.validators['job'].validate(value)
                # An ambiguous short form remains syntactically valid. A future
                # resolver must reject it and require one of the full forms.
                if not expected_short_allowed[short]:
                    self.assertGreater(len(candidates), 1)
                    self.assertTrue(all('/' in candidate for candidate in candidates))

    def test_project_owner_is_persistent_uuid_not_alias(self):
        self.validators['project'].validate(self.project)
        for owner in ('node-a', 'node@example', 'A' * 32, '1' * 31, None):
            with self.subTest(owner=owner):
                value = deepcopy(self.project)
                value['owner_node_id'] = owner
                with self.assertRaises(ValidationError):
                    self.validators['project'].validate(value)
        for mapping in ({'bad:token': '1' * 32}, {'node-a': 'node-a'}):
            value = deepcopy(self.project)
            value['legacy_nodes'] = mapping
            with self.assertRaises(ValidationError):
                self.validators['project'].validate(value)
        self.assertEqual(len(set(self.project['legacy_nodes'].values())),
                         len(self.project['legacy_nodes']))
        value['legacy_nodes'] = {}
        value['legacy_queues'] = {}
        self.validators['project'].validate(value)

    def test_legacy_queue_bindings_keep_unenrolled_uuid_null(self):
        queues = self.project['legacy_queues']['node-a']
        self.assertIsNone(queues['research']['queue_id'])
        self.assertIsInstance(queues['secondary']['queue_id'], str)
        self.validators['project'].validate(self.project)
        invalid_fields = (
            ('queue_id', 'research'),
            ('state_root', 'relative/queue'),
            ('root_identity', [1]),
            ('root_identity', [1, -1]),
            ('database_identity', [1, 2, 3]),
            ('database_identity', [1, 'inode']),
        )
        for field, invalid in invalid_fields:
            with self.subTest(field=field, invalid=invalid):
                value = deepcopy(self.project)
                value['legacy_queues']['node-a']['research'][field] = invalid
                with self.assertRaises(ValidationError):
                    self.validators['project'].validate(value)
        for field in ('queue_id', 'state_root', 'root_identity', 'database_identity'):
            value = deepcopy(self.project)
            del value['legacy_queues']['node-a']['research'][field]
            with self.assertRaises(ValidationError):
                self.validators['project'].validate(value)

    def test_operational_projection_preserves_unknown(self):
        mappings = {'Queue': 'queued', 'Hold': 'queued', 'Starting': 'queued', 'Run': 'running',
                    'Cancelling': 'running', 'Done': 'done', 'Fail': 'failed',
                    'Cancelled': 'cancelled', 'Unknown': None}
        for native, projected in mappings.items():
            with self.subTest(native=native):
                value = deepcopy(self.native['state'])
                value.update(native_status=native, status=projected)
                self.validators['state'].validate(value)
                value['status'] = 'done' if projected != 'done' else 'failed'
                with self.assertRaises(ValidationError):
                    self.validators['state'].validate(value)
        value.update(native_status=None, status=None, provenance='backfilled')
        self.validators['state'].validate(value)
        value['provenance'] = 'native'
        with self.assertRaises(ValidationError):
            self.validators['state'].validate(value)

    def test_scientific_and_cancel_enums_are_closed(self):
        for field, invalid in (('scf_result', 'accepted'), ('scf_result', None), ('cancel_mode', 'LABORT')):
            with self.subTest(field=field, invalid=invalid):
                value = deepcopy(self.native['state'])
                value[field] = invalid
                with self.assertRaises(ValidationError):
                    self.validators['state'].validate(value)

    def test_annotation_text_can_be_explicitly_cleared(self):
        value = deepcopy(self.native['annotations'])
        for field in ('purpose', 'system', 'note', 'label'):
            value['events'][0][field] = ''
        self.validators['annotations'].validate(value)
        for field in ('purpose', 'system', 'note', 'label'):
            value['events'][0][field] = None
        self.validators['annotations'].validate(value)

    def test_registration_binding_belongs_to_state(self):
        self.assertNotIn('binding', self.native['job'])
        self.assertIsInstance(self.native['state']['binding'], dict)
        value = deepcopy(self.native['job'])
        value['binding'] = self.native['state']['binding']
        with self.assertRaises(ValidationError):
            self.validators['job'].validate(value)
        state = deepcopy(self.native['state'])
        state['binding'] = None
        self.validators['state'].validate(state)
        del state['binding']
        with self.assertRaises(ValidationError):
            self.validators['state'].validate(state)

    def test_fixture_sets_share_identity_and_annotation_sequence(self):
        for directory in ('native', 'j27-backfill'):
            documents = {kind: read_json(FIXTURES / directory / '.cmw-manifest' / f'{kind}.json')
                         for kind in KINDS}
            for key in ('schema_version', 'id', 'project', 'provenance'):
                self.assertEqual(len({value[key] for value in documents.values()}), 1)
            annotations = documents['annotations']
            self.assertEqual(annotations['revision'], len(annotations['events']))
            self.assertEqual([event['sequence'] for event in annotations['events']],
                             list(range(1, annotations['revision'] + 1)))
        self.assertEqual(documents['job']['id'], 'node-a/project-queue:J27.1')
        self.assertEqual(documents['job']['parent'], 'node-a/project-queue:J16.1')
        self.assertIs(documents['job']['parent_inferred'], True)
        self.assertEqual(documents['state']['status'], 'done')
        self.assertEqual(documents['state']['scf_result'], 'labort')
        self.assertIsNone(documents['job']['restart_inputs']['WAVECAR'])
        self.assertEqual(documents['annotations']['events'], [])


if __name__ == '__main__':
    unittest.main()
