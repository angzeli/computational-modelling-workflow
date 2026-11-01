from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch

from cmw.core.provenance import file_hash
from cmw.jobs import molecular
from cmw.jobs.successor_orca import preview_orca, materialize_orca, verify_materialized_orca
from cmw.molecular.orca.geometry import OrcaGeometryInput
from cmw.molecular.orca.job import finalize_attempt
from cmw.molecular.orca.jobs import parse_input
from cmw.molecular.orca.status import StageType
from cmw.structure.xyz import geometry_hash


class SuccessorOrcaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        native = self.root / 'benign-unexecuted-orca'
        native.write_text('#!/bin/sh\nexit 0\n'); native.chmod(0o700)
        original = self.root / 'original.inp'
        original.write_text('! HF STO-3G OPT NoAutoStart\n%pal nprocs 1 end\n%maxcore 819\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n')
        self.spec = {'schema':1,'request_id':str(uuid4()),'engine':'ORCA','name':'parent','intent':'held',
            'resources':{'cpus':1,'memory_gib':1,'mpi_ranks':1,'threads_per_rank':1},
            'runtime':{'executable':str(native),'sha256':file_hash(native),'version':'6.1.1','qualification':'synthetic'},
            'workdir':str(self.root/'parent'), 'scientific':{'profile':'opt','input':str(original)}}
        prepared = molecular.prepare(self.spec)
        bound = molecular.binding(self.spec, prepared)
        self.target = {'node_id':str(uuid4()),'queue_id':str(uuid4()),'job_id':1,'attempt_id':str(uuid4())}
        self.job = {'id':1,'attempt_id':self.target['attempt_id'],'engine':'ORCA','resources':self.spec['resources'],
            'argv':molecular._stored_argv(bound['plan']['argv']), 'env':bound['plan']['env'],'cwd':bound['plan']['cwd'],
            'molecular_submission':bound,'status':'Fail'}
        self.parent = Path(self.spec['workdir'])
        (self.parent/'stage.xyz').write_text('2\nobserved complete\nH 0 0 0\nH 0 0 0.80\n')
        (self.parent/'stage.out').write_text(self.native_output())
        (self.parent/'stage.err').write_text('')
        self.template = self.root/'next.inp'
        self.template.write_text('! HF STO-3G OPT NoAutoStart\n%pal nprocs 1 end\n%maxcore 819\n* xyz 0 1\nH 0 0 0\nH 0 0 0.80\n*\n')
        config = self.root/'execution.json'
        config.write_text(json.dumps({'schema_version':1,'active_profile':'reviewed','profiles':{'reviewed':{
            'orca':{'nprocs':1,'total_memory_gb':1},'multiwfn':{'nthreads':1,'total_memory_gb':1}}}}))
        self.options = {'source':{'mode':'observed','input':str(self.parent/'stage.inp'),
            'target':str(self.parent/'target.json'),'geometry':str(self.parent/'stage.xyz'),
            'output':str(self.parent/'stage.out')}, 'template_input':str(self.template),
            'execution_config':str(config),'execution_profile':'reviewed','output':str(self.root/'successor')}
        self.kwargs = {'job':self.job,'state_directory':self.root/'state','runtime':self.spec['runtime'],'resources':self.spec['resources']}

    @staticmethod
    def native_output(*, accepted=False, energy=True):
        return ('Program Version 6.1.1\nCARTESIAN COORDINATES (ANGSTROEM)\n---------------------------------\n'
                'H 0.000000 0.000000 0.000000\nH 0.000000 0.000000 0.800000\n\n'
                + ('SCF CONVERGED AFTER 3 CYCLES\nFINAL SINGLE POINT ENERGY -1.1\n' if energy else '')
                + ('THE OPTIMIZATION HAS CONVERGED\n' if accepted else 'The optimization did not converge\n')
                + 'ORCA TERMINATED NORMALLY\n')

    def preview(self):
        return preview_orca(self.options, self.target, **self.kwargs)

    def test_observed_complete_is_not_optimized_and_preview_is_read_only(self):
        before = {path: file_hash(path) for path in self.parent.iterdir() if path.is_file()}
        first = self.preview()
        self.assertEqual(first, self.preview())
        self.assertEqual(first['source_qualification'], 'COMPLETE_OBSERVED_GEOMETRY_NOT_OPTIMIZED')
        self.assertEqual(first['selected_frame']['frame'], 1)
        self.assertFalse(Path(self.options['output']).exists())
        self.assertEqual(before, {path: file_hash(path) for path in before})
        self.assertEqual(first['checkpoint']['status'], 'REFUSED')
        self.assertTrue(any(row['category'] == 'geometry/cell' for row in first['changes']))

    def test_materializer_uses_existing_attempt_and_complete_checker(self):
        (self.parent/'stage.gbw').write_bytes(b'unchecked checkpoint must never be copied')
        preview = self.preview()
        result = materialize_orca(self.options, self.target, preview, **self.kwargs)
        record = result['materialization']
        self.assertEqual(record['attempt']['attempt_id'], 'attempt_001')
        parsed = parse_input(Path(result['scientific']['input']), StageType.OPT)
        self.assertEqual(geometry_hash(parsed['geometry']), preview['source_geometry_sha256'])
        self.assertEqual(parsed['resources'].maxcore_mb_per_process, 819)
        self.assertEqual(list(Path(self.options['output']).rglob('*.gbw')), [])
        new_spec = {**self.spec,'request_id':str(uuid4()),'name':'successor','workdir':result['workdir'],'scientific':result['scientific']}
        bound = molecular.binding(new_spec, result['prepared_plan'])
        self.assertEqual(bound['plan']['scientific_association']['target_id'], preview['target_id'])
        self.assertIn('--layout',bound['plan']['argv'])
        with self.assertRaisesRegex(ValueError, 'no-clobber'):
            materialize_orca(self.options,self.target,preview,**self.kwargs)

    def test_source_change_after_approval_refuses_before_creation(self):
        approved = self.preview()
        with (self.parent/'stage.out').open('a') as stream:
            stream.write('modified after preview\n')
        with self.assertRaisesRegex(ValueError, 'changed before materialization'):
            materialize_orca(self.options,self.target,approved,**self.kwargs)
        self.assertFalse(Path(self.options['output']).exists())

    def test_source_mutation_during_publication_retains_failure_without_registration(self):
        from cmw.core.plan_materialization import WorkflowPlanMaterializer
        original = WorkflowPlanMaterializer.materialize_node
        def mutate(materializer, *args, **kwargs):
            record = original(materializer, *args, **kwargs)
            with (self.parent/'stage.out').open('a') as stream:
                stream.write('changed during materialization\n')
            return record
        approved = self.preview()
        with patch.object(WorkflowPlanMaterializer, 'materialize_node', mutate):
            with self.assertRaisesRegex(ValueError, 'source changed during materialization'):
                materialize_orca(self.options,self.target,approved,**self.kwargs)
        failure = json.loads((Path(self.options['output'])/'successor-failure.json').read_text())
        self.assertEqual(failure['status'],'INCOMPLETE')
        self.assertFalse((self.root/'state').exists())
        self.assertTrue(list(Path(self.options['output']).rglob('materialization.json')))

    def test_parent_and_state_publication_paths_are_refused_read_only(self):
        before = file_hash(self.parent/'stage.inp')
        for destination in (self.parent/'child',self.root/'state'/'child',self.parent):
            with self.subTest(destination=destination):
                self.options['output'] = str(destination)
                with self.assertRaisesRegex(ValueError,'no-clobber'):
                    self.preview()
                self.assertFalse(destination.exists() and destination != self.parent)
        self.assertEqual(file_hash(self.parent/'stage.inp'),before)

    def test_partial_and_unevaluated_geometry_refused(self):
        for output in (self.native_output(energy=False), self.native_output().replace('ENERGY -1.1', 'ENERGY ...'),self.native_output().replace('H 0.000000 0.000000 0.800000\n','')):
            with self.subTest(output=output):
                (self.parent/'stage.out').write_text(output)
                with self.assertRaisesRegex(ValueError, 'complete, unambiguous evaluated'):
                    self.preview()
        (self.parent/'stage.out').write_text(self.native_output() + '\nCARTESIAN COORDINATES (ANGSTROEM)\n----\nH 0.000000 0.000000 0.000000\nH 0.000000 0.000000 0.900000\n\n')
        (self.parent/'stage.xyz').write_text('2\nlater unevaluated\nH 0 0 0\nH 0 0 0.9\n')
        with self.assertRaisesRegex(ValueError, 'complete, unambiguous evaluated'):
            self.preview()

    def test_energy_in_a_later_cycle_cannot_certify_an_unevaluated_frame(self):
        incomplete = self.native_output(energy=False).split('The optimization')[0]
        (self.parent/'stage.out').write_text(incomplete + 'GEOMETRY OPTIMIZATION CYCLE 2\n'
            'SCF CONVERGED AFTER 3 CYCLES\nFINAL SINGLE POINT ENERGY -1.2\nORCA TERMINATED NORMALLY\n')
        with self.assertRaisesRegex(ValueError,'complete, unambiguous evaluated'):
            self.preview()

    def test_checkpoint_constructs_and_missing_noautostart_refused(self):
        for replacement in ('','MOREAD','NoAutoStart\n%moinp "missing.gbw"'):
            with self.subTest(replacement=replacement):
                self.template.write_text(self.template.read_text().replace('NoAutoStart',replacement))
                with self.assertRaises(ValueError):
                    self.preview()
                self.template.write_text('! HF STO-3G OPT NoAutoStart\n%pal nprocs 1 end\n%maxcore 819\n* xyz 0 1\nH 0 0 0\nH 0 0 0.80\n*\n')

    def test_order_or_template_geometry_mismatch_refused(self):
        self.template.write_text(self.template.read_text().replace('H 0 0 0.80','H 0 0 0.81'))
        with self.assertRaisesRegex(ValueError,'coordinates must exactly equal'):
            self.preview()
        (self.parent/'stage.xyz').write_text('2\nwrong order\nHe 0 0 0\nH 0 0 0.8\n')
        with self.assertRaisesRegex(ValueError,'atom/species order'):
            self.preview()

    def test_explicit_spin_charge_solver_and_stage_changes_are_scientific(self):
        text = self.template.read_text().replace('OPT NoAutoStart','SP NoAutoStart TightSCF').replace('* xyz 0 1','* xyz 1 2')
        self.template.write_text(text)
        preview = self.preview()
        changes = {(row['category'],row['field']) for row in preview['changes']}
        self.assertIn(('Hamiltonian/spin/charge','charge'),changes)
        self.assertIn(('Hamiltonian/spin/charge','multiplicity'),changes)
        self.assertIn(('solver/convergence controls','numerical_settings'),changes)
        self.assertIn(('scientific_stage','stage'),changes)

    def test_profile_does_not_silently_replace_maxcore(self):
        self.template.write_text(self.template.read_text().replace('%maxcore 819','%maxcore 800'))
        with self.assertRaisesRegex(ValueError,'PAL/MaxCore'):
            self.preview()

    def test_identical_bytes_distinct_parent_identity_changes_plan(self):
        first = self.preview()
        self.target = {**self.target,'attempt_id':str(uuid4())}
        self.job['attempt_id'] = self.target['attempt_id']
        second = self.preview()
        self.assertNotEqual(first['execution_plan']['execution_plan_id'],second['execution_plan']['execution_plan_id'])
        self.assertEqual(first['target_id'],second['target_id'])

    def test_accepted_requires_real_existing_finalizer_and_opt_marker(self):
        (self.parent/'stage.out').write_text(self.native_output(accepted=True))
        geometry = OrcaGeometryInput.from_mapping(json.loads((self.parent/'geometry-input.json').read_text()))
        finalize_attempt(target_path=self.parent/'target.json',metadata_path=self.parent/'job.json',input_path=self.parent/'stage.inp',
            output_path=self.parent/'stage.out',stderr_path=self.parent/'stage.err',process_exit_code=0,
            executable={'path':self.spec['runtime']['executable']},resources={'nprocs':1,'maxcore_mb_per_process':819},
            artifacts={'final_geometry':self.parent/'stage.xyz'},geometry_input=geometry,geometry_contract_path=self.parent/'geometry-input.json')
        self.options['source'].update(mode='accepted',result=str(self.parent/'job.json'))
        self.assertEqual(self.preview()['source_qualification'],'FINALIZED_OPTIMIZED_GEOMETRY')
        (self.parent/'stage.out').write_text(self.native_output(accepted=False))
        with self.assertRaisesRegex(ValueError,'finalizer/reuse'):
            self.preview()

    def test_materialized_sp_is_a_supported_parent_without_repreparation(self):
        self.template.write_text(self.template.read_text().replace('OPT NoAutoStart', 'SP NoAutoStart'))
        result = materialize_orca(self.options,self.target,self.preview(),**self.kwargs)
        next_spec = {**self.spec,'request_id':str(uuid4()),'name':'SP successor','workdir':result['workdir'],'scientific':result['scientific']}
        bound = molecular.binding(next_spec,result['prepared_plan'])
        target = {**self.target,'job_id':2,'attempt_id':str(uuid4())}
        job = {**self.job,'id':2,'attempt_id':target['attempt_id'],'argv':molecular._stored_argv(bound['plan']['argv']),
               'env':bound['plan']['env'],'cwd':bound['plan']['cwd'],'molecular_submission':bound}
        argv = bound['plan']['argv']
        options = {**self.options,'output':str(self.root/'second-successor'),'source':{
            'mode':'input','input':result['scientific']['input'],
            'target':argv[argv.index('--target')+1],
            'geometry':result['materialization']['input_files']['geometry']}}
        preview = preview_orca(options,target,**{**self.kwargs,'job':job})
        self.assertEqual(preview['source_qualification'],'REVIEWED_INPUT_GEOMETRY')
        self.assertEqual(preview['target_id'],result['materialization']['target']['target_id'])

    def test_low_precision_or_constrained_geometry_is_not_guessed(self):
        (self.parent/'stage.out').write_text(self.native_output().replace('0.800000','0.8'))
        with self.assertRaisesRegex(ValueError,'complete, unambiguous evaluated'):
            self.preview()
        (self.parent/'stage.out').write_text(self.native_output())
        with self.template.open('a') as stream:
            stream.write('%geom Constraints { C 0 C } end end\n')
        with self.assertRaisesRegex(ValueError,'Unsupported ORCA block'):
            self.preview()

    def test_registration_rederives_argv_and_original_materializer_contract(self):
        approved = self.preview()
        result = materialize_orca(self.options,self.target,approved,**self.kwargs)
        self.assertEqual(verify_materialized_orca(self.options,self.target,approved,result,**self.kwargs),result)
        for field, replacement in (('argv',['/bin/sh','-c','echo forged']),('env',{'INJECTED':'1'}),('progress_sources',[])):
            with self.subTest(field=field):
                forged = deepcopy(result);forged['prepared_plan'][field]=replacement
                with self.assertRaisesRegex(ValueError,'rederived approved materialization'):
                    verify_materialized_orca(self.options,self.target,approved,forged,**self.kwargs)
        input_path = Path(result['scientific']['input'])
        input_path.write_text(input_path.read_text().replace('HF STO-3G','PBE STO-3G'))
        with self.assertRaisesRegex(ValueError,'attempt evidence differs'):
            verify_materialized_orca(self.options,self.target,approved,result,**self.kwargs)

    def test_source_requires_registered_original_path(self):
        copied = self.root/'copied.xyz';copied.write_bytes((self.parent/'stage.xyz').read_bytes())
        self.options['source']['geometry']=str(copied)
        with self.assertRaisesRegex(ValueError,'exact registered'):
            self.preview()

    def test_input_retry_keeps_input_identity(self):
        self.options['source'].update(mode='input',geometry=str(self.parent/'input.xyz'))
        self.options['source'].pop('output')
        self.template.write_text(self.template.read_text().replace('0.80','0.74'))
        self.assertEqual(self.preview()['source_qualification'],'REVIEWED_INPUT_GEOMETRY')


if __name__ == '__main__':
    unittest.main()
