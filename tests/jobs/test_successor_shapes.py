"""Malformed caller specifications refuse before parent or engine inspection."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.jobs.store import JobsError, Store
from cmw.jobs.successor import SuccessorService, _spec


def specification():
    return {'schema': 1, 'parent': {'node_id': uuid4().hex, 'queue_id': uuid4().hex,
        'job_id': 1, 'attempt_id': uuid4().hex}, 'mode': 'geometry', 'engine': 'VASP',
        'name': 'Explicit fixture', 'checkpoint': 'none', 'approved_changes': [],
        'resources': {'cpus': 1, 'memory_gib': 1., 'mpi_ranks': 1, 'threads_per_rank': 1},
        'runtime': {'node_config': '/owner/node', 'profile': 'vasp', 'vasp': {}, 'run_directory': '/owner/run'},
        'adapter': {'directory': '/owner/parent', 'preparation_spec': '/owner/preparation.json',
            'output': '/owner/new', 'scratch_root': '/owner/scratch', 'record_directory': 'record', 'source_mode': 'observed'}}


class SuccessorShapeTests(unittest.TestCase):
    def test_unhashable_top_level_values_are_explicit_refusals(self):
        for field, value in (('mode', []), ('engine', {}), ('approved_changes', [{}]),
                             ('parent', {}), ('resources', []), ('runtime', []), ('adapter', [])):
            spec = specification(); spec[field] = value
            with self.subTest(field=field), self.assertRaises(JobsError):
                _spec(spec)
        for key, value in (('node_id', []), ('job_id', True), ('attempt_id', 'not-an-attempt')):
            spec = specification(); spec['parent'][key] = value
            with self.subTest(key=key), self.assertRaises(JobsError):
                _spec(spec)

    def test_vasp_missing_or_malformed_adapter_runtime_fields_refuse_before_read(self):
        candidates = []
        for field in ('directory', 'output', 'preparation_spec'):
            spec = specification(); del spec['adapter'][field]; candidates.append(spec)
        for field, value in (('directory', {}), ('source_mode', []), ('allow_structure_changes', [{}])):
            spec = specification(); spec['adapter'][field] = value; candidates.append(spec)
        for field, value in (('profile', {}), ('node_config', None), ('vasp', None), ('run_directory', [])):
            spec = specification(); spec['runtime'][field] = value; candidates.append(spec)
        with tempfile.TemporaryDirectory(prefix='cmw-successor-shape-') as temporary:
            root = Path(temporary).resolve(); path = root / 'spec.json'
            for spec in candidates:
                path.write_text(json.dumps(spec))
                with self.subTest(spec=spec), patch('cmw.jobs.successor.read_job') as read:
                    with self.assertRaises(JobsError):
                        SuccessorService(Store(root / 'missing')).plan(1, str(path))
                    read.assert_not_called()
            self.assertFalse((root / 'missing').exists())

    def test_orca_container_fields_are_checked_without_scientific_defaults(self):
        base = specification(); base['engine'] = 'ORCA'
        base['runtime'] = {'executable': '/owner/orca', 'sha256': 'a'*64, 'version': '6.1.0', 'qualification': 'synthetic'}
        base['adapter'] = {'source': {'mode': 'observed', 'input': '/owner/input', 'target': '/owner/target',
            'geometry': '/owner/geometry', 'output': '/owner/output'}, 'template_input': '/owner/template',
            'execution_config': '/owner/config', 'execution_profile': 'explicit', 'output': '/owner/new'}
        self.assertEqual(_spec(base), base)
        for group, key, value in (('adapter', 'source', []), ('adapter', 'execution_profile', {}),
                                   ('runtime', 'qualification', []), ('runtime', 'version', None)):
            spec = deepcopy(base); spec[group][key] = value
            with self.subTest(key=key), self.assertRaises(JobsError):
                _spec(spec)
        for key in ('source', 'template_input', 'execution_config', 'execution_profile', 'output'):
            spec = deepcopy(base); del spec['adapter'][key]
            with self.subTest(key=key), self.assertRaises(JobsError):
                _spec(spec)
        for key, value in (('mode', []), ('geometry', {})):
            spec = deepcopy(base); spec['adapter']['source'][key] = value
            with self.subTest(key=key), self.assertRaises(JobsError):
                _spec(spec)

    def test_malformed_content_bound_record_refuses_without_queue_access(self):
        from cmw.core.provenance import stable_hash
        with tempfile.TemporaryDirectory(prefix='cmw-successor-record-') as temporary:
            root=Path(temporary).resolve(); path=root/'record.json'
            for plan in ({}, {'spec': []}, {'spec': specification()}):
                digest=stable_hash(plan)
                path.write_text(json.dumps({'kind':'cmw-successor-materialization','status':'MATERIALIZED',
                    'record_path':str(path),'plan_digest':digest,'plan':plan}))
                with self.subTest(plan=plan), patch('cmw.jobs.successor.read_job') as read:
                    with self.assertRaises(JobsError):
                        SuccessorService(Store(root/'missing')).register(1,str(path),digest,uuid4().hex)
                    read.assert_not_called()
            self.assertFalse((root/'missing').exists())
