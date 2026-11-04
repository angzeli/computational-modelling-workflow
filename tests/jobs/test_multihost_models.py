"""Synthetic wire contracts; no processes, transport, or potential files."""
from copy import deepcopy
import json
from pathlib import Path
import re
import unittest

from cmw.jobs.multihost import models as m


UTC = '2026-11-04T12:00:00.000Z'
SHA = 'a' * 64


def capabilities():
    return {'submit': True, 'receive': {'max_bundle_bytes': 67108864},
            'cancel_modes': {'vasp': ['graceful', 'soft', 'hard'], 'orca': ['hard']},
            'engines': {'vasp': {'builds': [
                {'build_id': 'synthetic-production', 'version': '6.6.1',
                 'executables': ['vasp_std', 'vasp_gam', 'vasp_ncl'],
                 'experimental': False, 'default': True}]}}}


def hello():
    return {'node_id': 'mm_m5pro', 'protocol_version': '1.0', 'cmw_version': '0.1.0',
            'cmw_commit': 'synthetic-commit', 'node_time_utc': UTC,
            'hardware': {'physical_cores': 18,
                         'core_tiers': [{'level': 0, 'name': 'Native Tier Label', 'physical_cores': 12}],
                         'recommended_max_parallelism': None}, 'capabilities': capabilities()}


def manifest():
    return {'schema_version': '1.0', 'request_id': '1' * 32, 'source_node_id': 'mbp_m3max',
            'created_utc': UTC, 'engine': 'vasp', 'executable': 'vasp_std',
            'build': {'build_id': None, 'allow_experimental': False},
            'execution': {'mpi_ranks': 16, 'omp_threads': 1},
            'files': [{'path': path, 'size': 12, 'sha256': SHA} for path in m.INPUT_PATHS],
            'provenance': {'preparation_record_sha256': SHA, 'potcar_identity': [
                {'element': 'H', 'variant': 'H', 'titel': 'SYNTHETIC H identity only', 'sha256': SHA}]}}


def cancel():
    return {'mode': 'soft', 'state': 'in_progress', 'history': [
        {'mode': 'graceful', 'requested_utc': UTC, 'by_node_id': 'mm_m5pro'},
        {'mode': 'soft', 'requested_utc': UTC, 'by_node_id': 'mbp_m3max'}]}


def envelope():
    return {'protocol_version': '1.0', 'node_id': 'mm_m5pro', 'command': 'hello',
            'ok': True, 'result': hello(), 'error': None}


class ModelTests(unittest.TestCase):
    def test_round_trip_all_top_level_models(self):
        for cls, value in ((m.Envelope, envelope()), (m.HelloResult, hello()),
                           (m.Manifest, manifest()), (m.Capabilities, capabilities()),
                           (m.CancelState, cancel())):
            with self.subTest(model=cls.__name__):
                parsed = cls.from_json(json.dumps(value))
                self.assertEqual(parsed.to_dict(), value)
                self.assertEqual(cls.from_json(parsed.to_json()), parsed)

    def test_unknown_major_is_typed_and_minor_is_retained(self):
        for cls, factory, field in ((m.Envelope, envelope, 'protocol_version'),
                                    (m.HelloResult, hello, 'protocol_version'),
                                    (m.Manifest, manifest, 'schema_version')):
            with self.subTest(model=cls.__name__):
                value = factory()
                value[field] = '2.0'
                with self.assertRaises(m.UnsupportedVersionError) as caught:
                    cls.from_dict(value)
                self.assertEqual(caught.exception.code, 'protocol_incompatible')
                self.assertEqual(caught.exception.version, '2.0')
                value[field] = '1.25'
                if cls is m.Envelope:
                    value['result']['protocol_version'] = '1.25'
                self.assertEqual(cls.from_dict(value).to_dict()[field], '1.25')

    def test_malformed_versions(self):
        for version in (1, True, '1', '1.0.0', '01.0', '1.-1', '1.00', '1.0\n'):
            with self.subTest(version=version):
                value = hello()
                value['protocol_version'] = version
                with self.assertRaises(m.ContractError):
                    m.HelloResult.from_dict(value)

    def test_json_rejects_duplicates_constants_and_nonobjects(self):
        for payload in ('{"mode":"hard","mode":"soft"}', '{"a":NaN}',
                        '{"a":Infinity}', 'null', '[]', '{', b'\xff'):
            with self.subTest(payload=payload):
                with self.assertRaises(m.ContractError):
                    m.CancelState.from_json(payload)

    def test_all_error_codes_and_exit_codes(self):
        self.assertEqual(list(m.ERROR_EXIT_CODES.values()), [2, 10, 11, 20, 21, 22, 40])
        for code in m.ErrorCode:
            value = envelope()
            value.update(ok=False, result=None, error={
                'code': code.value, 'message': 'Synthetic refusal', 'details': {'count': 1}})
            parsed = m.Envelope.from_dict(value)
            self.assertEqual(parsed.error.code, code)
            self.assertEqual(m.Envelope.from_json(parsed.to_json()), parsed)

    def test_envelope_result_and_error_invariants(self):
        failures = [dict(ok=1), dict(error={}), dict(result=None), dict(command='shell'),
                    dict(request_id=None), dict(request_id=''), dict(node_id='Bad-Node')]
        for changes in failures:
            with self.subTest(changes=changes):
                value = envelope()
                value.update(changes)
                with self.assertRaises(m.ContractError):
                    m.Envelope.from_dict(value)
        value = envelope()
        value.update(ok=False, result=None, error={'code':'unknown', 'message':'x', 'details':{}})
        with self.assertRaises(m.ContractError):
            m.Envelope.from_dict(value)

    def test_hello_envelope_identity_and_version_must_agree(self):
        for field, replacement in (('node_id', 'mbp_m3max'), ('protocol_version', '1.1')):
            value = envelope()
            value['result'][field] = replacement
            with self.assertRaises(m.ContractError):
                m.Envelope.from_dict(value)

    def test_optional_echo_and_capability_extensions(self):
        value = envelope()
        value['request_id'] = 'request echoed literally'
        self.assertEqual(m.Envelope.from_dict(value).to_dict(), value)
        self.assertNotIn('request_id', m.Envelope.from_dict(envelope()).to_dict())
        self.assertEqual(m.Capabilities.from_dict({}).to_dict(), {})
        value = {'submit': False, 'future_feature': {'versions':[1, 2]}}
        self.assertEqual(m.Capabilities.from_dict(value).to_dict(), value)
        for field in m.Capabilities.KNOWN:
            with self.subTest(field=field):
                with self.assertRaises(m.ContractError):
                    m.Capabilities.from_dict({field: None})

    def test_hello_numeric_and_capability_types(self):
        for number in (True, 0, -1, 1.5, '18'):
            value = hello()
            value['hardware']['physical_cores'] = number
            with self.assertRaises(m.ContractError):
                m.HelloResult.from_dict(value)
        for changes in ({'submit': 1}, {'receive': {'max_bundle_bytes': False}},
                        {'cancel_modes': {'vasp':['hard', 'hard']}},
                        {'cancel_modes': {'vasp':['force']}}, {'engines': []}):
            with self.subTest(changes=changes):
                with self.assertRaises(m.ContractError):
                    m.Capabilities.from_dict(changes)

    def test_exact_four_file_manifest(self):
        for paths in (m.INPUT_PATHS[:-1], (*m.INPUT_PATHS, 'inputs/extra'),
                      (*m.INPUT_PATHS[:-1], m.INPUT_PATHS[0]),
                      ('../INCAR', *m.INPUT_PATHS[1:]),
                      ('/inputs/INCAR', *m.INPUT_PATHS[1:])):
            value = manifest()
            value['files'] = [{'path': path, 'size': 12, 'sha256': SHA} for path in paths]
            with self.subTest(paths=paths), self.assertRaises(m.ContractError):
                m.Manifest.from_dict(value)

    def test_manifest_scalar_constraints(self):
        mutations = ((('execution','mpi_ranks'), True), (('execution','mpi_ranks'), 0),
                     (('execution','omp_threads'), 1.5), (('build','allow_experimental'), 1),
                     (('source_node_id',), 'bad-node'), (('request_id',), 'not-uuid'),
                     (('engine',), 'orca'), (('executable',), '/bin/vasp_std'),
                     (('files',0,'sha256'), 'g'*64), (('files',0,'size'), -1),
                     (('provenance','potcar_identity'), []))
        for path, replacement in mutations:
            with self.subTest(path=path, value=replacement):
                value = manifest()
                target = value
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = replacement
                with self.assertRaises(m.ContractError):
                    m.Manifest.from_dict(value)

    def test_utc_timestamp_validation(self):
        for stamp in ('2026-02-30T12:00:00Z', '2026-11-04T12:00:00',
                      '2026-11-04T12:00:00+01:00', '2026-11-04 12:00:00Z'):
            value = hello()
            value['node_time_utc'] = stamp
            with self.subTest(stamp=stamp), self.assertRaises(m.ContractError):
                m.HelloResult.from_dict(value)

    def test_cancel_shape_modes_and_states(self):
        for state in ('requested', 'in_progress', 'completed', 'failed'):
            value = cancel()
            value['state'] = state
            self.assertEqual(m.CancelState.from_dict(value).state, state)
        for change in ({'mode':'force'}, {'state':'done'}, {'history':[]}, {'mode':None}):
            value = cancel()
            value.update(change)
            with self.assertRaises(m.ContractError):
                m.CancelState.from_dict(value)

    def test_cancel_history_is_strict_escalation_with_effective_final_mode(self):
        from itertools import combinations
        modes = [mode.value for mode in m.CancelMode]
        for length in (1, 2, 3):
            for sequence in combinations(modes, length):
                value = cancel()
                value['mode'] = sequence[-1]
                value['history'] = [{'mode': mode, 'requested_utc': UTC, 'by_node_id': 'mm_m5pro'}
                                    for mode in sequence]
                self.assertEqual(m.CancelState.from_dict(value).mode, sequence[-1])
        for sequence, effective in ((['soft', 'graceful'], 'graceful'),
                                    (['hard', 'hard'], 'hard'), (['graceful'], 'hard')):
            value = cancel()
            value['mode'] = effective
            value['history'] = [{'mode': mode, 'requested_utc': UTC, 'by_node_id': 'mm_m5pro'}
                                for mode in sequence]
            with self.subTest(sequence=sequence, effective=effective), self.assertRaises(m.ContractError):
                m.CancelState.from_dict(value)

    def test_native_hardware_tiers_are_preserved_without_interpretation(self):
        value = hello()['hardware']
        value['core_tiers'] = [
            {'level': 0, 'name': '  Native SUPER Tier  ', 'physical_cores': 8},
            {'level': 1, 'name': 'Unrecognized Label Ω', 'physical_cores': 8},
            {'level': 2, 'name': 'Efficiency', 'physical_cores': 2}]
        value['recommended_max_parallelism'] = 16
        parsed = m.Hardware.from_dict(value)
        self.assertEqual(parsed.to_dict(), value)
        value['core_tiers'] = []
        value['recommended_max_parallelism'] = None
        self.assertEqual(m.Hardware.from_dict(value).to_dict(), value)
        for changes in ({'recommended_max_parallelism': False}, {'recommended_max_parallelism': 0},
                        {'core_tiers': [{'level': -1, 'name': 'Native', 'physical_cores': 2}]},
                        {'performance_cores': 12}):
            with self.subTest(changes=changes), self.assertRaises(m.ContractError):
                m.Hardware.from_dict({**value, **changes})

    def test_errors_do_not_echo_payload(self):
        value = manifest()
        value['engine'] = 'SECRET_PAYLOAD'
        with self.assertRaises(m.ContractError) as caught:
            m.Manifest.from_dict(value)
        self.assertNotIn('SECRET_PAYLOAD', str(caught.exception))

    def test_serialization_revalidates_python_construction(self):
        with self.assertRaises(m.ContractError):
            m.Hardware(True, (), None).to_json()

    def test_json_schema_integer_semantics_normalize_integral_numbers(self):
        value = manifest()
        value['execution']['omp_threads'] = 1.0
        self.assertIs(type(m.Manifest.from_dict(value).execution.omp_threads), int)
        value = hello()['hardware']
        value['physical_cores'] = 18.0
        self.assertEqual(m.Hardware.from_json(json.dumps(value)).physical_cores, 18)


class SchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = Path(m.__file__).parent / 'schemas'
        cls.schemas = {path.name: json.loads(path.read_text()) for path in directory.glob('*.json')}

    def test_schema_files_are_json_and_draft_2020_12(self):
        self.assertEqual(set(self.schemas), {'response-envelope.schema.json', 'hello.schema.json',
                                           'manifest.schema.json', 'cancel-state.schema.json'})
        for name, value in self.schemas.items():
            with self.subTest(schema=name):
                self.assertEqual(value['$schema'], 'https://json-schema.org/draft/2020-12/schema')
                self.assertEqual(value['type'], 'object')
                self.assertEqual(value['additionalProperties'], False)

    def test_recursive_required_fields_match_models_and_missing_fields_fail(self):
        h, mf, c, e = hello(), manifest(), cancel(), envelope()
        cases = (
            ('response-envelope.schema.json', None, m.Envelope, e),
            ('response-envelope.schema.json', 'error', m.ErrorObject,
             {'code':'conflict', 'message':'Synthetic refusal', 'details':{}}),
            ('hello.schema.json', None, m.HelloResult, h),
            ('hello.schema.json', 'hardware', m.Hardware, h['hardware']),
            ('hello.schema.json', 'core_tier', m.CoreTier, h['hardware']['core_tiers'][0]),
            ('hello.schema.json', 'capabilities', m.Capabilities, h['capabilities']),
            ('hello.schema.json', 'receive', m.ReceiveCapability, h['capabilities']['receive']),
            ('hello.schema.json', 'engine', m.EngineCapabilities, h['capabilities']['engines']['vasp']),
            ('hello.schema.json', 'build', m.Build, h['capabilities']['engines']['vasp']['builds'][0]),
            ('manifest.schema.json', None, m.Manifest, mf),
            ('manifest.schema.json', 'build_selection', m.BuildSelection, mf['build']),
            ('manifest.schema.json', 'execution', m.Execution, mf['execution']),
            ('manifest.schema.json', 'file', m.ManifestFile, mf['files'][0]),
            ('manifest.schema.json', 'provenance', m.Provenance, mf['provenance']),
            ('manifest.schema.json', 'potcar_identity', m.PotcarIdentity, mf['provenance']['potcar_identity'][0]),
            ('cancel-state.schema.json', None, m.CancelState, c),
            ('cancel-state.schema.json', 'event', m.CancelEvent, c['history'][0]),
        )
        for name, definition, model, fixture in cases:
            schema = self.schemas[name] if definition is None else self.schemas[name]['$defs'][definition]
            with self.subTest(model=model.__name__):
                self.assertEqual(set(schema['required']), set(model.REQUIRED))
                self.assertLessEqual(set(schema['required']), set(schema['properties']))
                model.from_dict(fixture)
                for field in schema['required']:
                    incomplete = deepcopy(fixture)
                    del incomplete[field]
                    with self.assertRaises(m.ContractError, msg=f'{model.__name__}.{field}'):
                        model.from_dict(incomplete)
                if schema['additionalProperties'] is False:
                    with self.assertRaises(m.ContractError):
                        model.from_dict({**fixture, 'unexpected': True})

    def test_schema_references_resolve_locally(self):
        def visit(value, root):
            if isinstance(value, list):
                for item in value:
                    visit(item, root)
            elif isinstance(value, dict):
                if '$ref' in value:
                    ref = value['$ref']
                    if ref.startswith('#/$defs/'):
                        self.assertIn(ref.split('/')[-1], root['$defs'])
                    else:
                        self.assertIn(ref, self.schemas)
                for item in value.values():
                    visit(item, root)
        for schema in self.schemas.values():
            visit(schema, schema)

    def test_schema_enum_and_fixed_file_set_agree(self):
        error = self.schemas['response-envelope.schema.json']['$defs']['error']
        self.assertEqual(set(error['properties']['code']['enum']), {code.value for code in m.ErrorCode})
        schema = self.schemas['manifest.schema.json']
        self.assertEqual(set(schema['$defs']['file']['properties']['path']['enum']), set(m.INPUT_PATHS))
        paths = {item['contains']['properties']['path']['const'] for item in schema['properties']['files']['allOf']}
        self.assertEqual(paths, set(m.INPUT_PATHS))
        cancel_schema = self.schemas['cancel-state.schema.json']
        self.assertEqual(set(cancel_schema['properties']['mode']['enum']), {mode.value for mode in m.CancelMode})

    def test_schema_patterns_reject_terminal_newlines(self):
        cases = ((self.schemas['hello.schema.json']['properties']['node_id'], 'mm_m5pro'),
                 (self.schemas['hello.schema.json']['properties']['protocol_version'], '1.0'),
                 (self.schemas['hello.schema.json']['properties']['node_time_utc'], UTC),
                 (self.schemas['manifest.schema.json']['properties']['request_id'], '1'*32),
                 (self.schemas['manifest.schema.json']['$defs']['file']['properties']['sha256'], SHA))
        for schema, value in cases:
            self.assertIsNotNone(re.search(schema['pattern'], value))
            self.assertIsNone(re.search(schema['pattern'], value + '\n'))

    def test_schema_cancel_histories_match_the_escalation_sequences(self):
        from itertools import combinations
        schema = self.schemas['cancel-state.schema.json']
        expected = {sequence for length in (1, 2, 3)
                    for sequence in combinations(('graceful', 'soft', 'hard'), length)}
        actual = set()
        for option in schema['oneOf']:
            props = option['properties']
            sequence = tuple(item['properties']['mode']['const'] for item in props['history']['prefixItems'])
            self.assertEqual(props['mode']['const'], sequence[-1])
            self.assertEqual(props['history']['minItems'], len(sequence))
            self.assertEqual(props['history']['maxItems'], len(sequence))
            actual.add(sequence)
        self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()
