from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cmw.core.provenance import file_hash, stable_hash
from cmw.jobs.location_resolvers import orca_workflow_state_locations
from cmw.molecular.orca.jobs import check, prepare
from cmw.molecular.orca.runtime import inspect_recorded_orca_runtime, OrcaRuntimeError


class OrcaJobsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cmw-orca-jobs-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.native = self.root / 'native substitute'
        self.native.write_text('#!/bin/sh\nexit 91\n')
        self.native.chmod(0o755)
        self.input = self.root / 'source input.inp'
        self.input.write_text('! HF STO-3G SP\n%pal nprocs 1 end\n%maxcore 100\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n')
        self.spec = {'engine':'ORCA','resources':{'cpus':1,'memory_gib':1,'mpi_ranks':1,'threads_per_rank':1},
                     'workdir':str(self.root/'attempt Ω'),
                     'runtime':{'executable':str(self.native),'sha256':file_hash(self.native),'version':'6.1.1','qualification':'synthetic'},
                     'scientific':{'profile':'sp','input':str(self.input)}}

    def change_input(self, old, new):
        self.input.write_text(self.input.read_text().replace(old,new))

    def assert_status(self,status,fragment):
        result=check(self.spec)
        self.assertEqual(result['status'],status,result)
        self.assertIn(fragment,result['findings'][0]['message'])

    def test_check_is_read_only_and_uses_no_process_probe(self):
        with patch('subprocess.run',side_effect=AssertionError('unexpected probe')):
            result=check(self.spec)
        self.assertEqual(result['status'],'READY',result)
        self.assertFalse(Path(self.spec['workdir']).exists())
        self.assertEqual(result['plan']['progress_sources'][0]['stage'],'SP')
        self.assertFalse(result['plan']['suspension']['eligible'])
        self.assertIn('20%',result['plan']['memory_semantics'])

    def test_missing_xyz_is_rejected(self):
        self.input.write_text('! HF STO-3G SP\n%pal nprocs 1 end\n%maxcore 100\n* xyzfile 0 1 missing.xyz\n')
        self.assert_status('ERROR','missing.xyz')

    def test_external_xyz_spaces_and_unicode_are_explicit_dependencies(self):
        geometry=self.root/'geometry Ω.xyz'
        geometry.write_text('2\nsource\nH 0 0 0\nH 0 0 0.74\n')
        self.input.write_text('! HF STO-3G SP\n%pal nprocs 1 end\n%maxcore 100\n* xyzfile 0 1 "geometry Ω.xyz"\n')
        result=check(self.spec)
        self.assertEqual(result['status'],'READY',result)
        self.assertIn(str(geometry),[item['path'] for item in result['plan']['dependencies']])

    def test_incomplete_inline_geometry_and_nonfinite_are_errors(self):
        self.change_input('\n*\n','\n')
        self.assert_status('ERROR','closing *')
        self.input.write_text('! HF STO-3G SP\n%pal nprocs 1 end\n%maxcore 100\n* xyz 0 1\nH NaN 0 0\n*\n')
        self.assert_status('ERROR','finite')

    def test_duplicate_pal_and_resource_mismatch_fail(self):
        self.change_input('STO-3G SP','STO-3G SP PAL2')
        self.assert_status('ERROR','PAL')
        self.change_input(' SP PAL2',' SP')
        self.spec['resources']['cpus']=2
        self.assert_status('ERROR','commitments')

    def test_memory_headroom_and_nonfinite_commitments(self):
        self.spec['resources']['memory_gib']=100/1024
        self.assert_status('ERROR','headroom')
        self.spec['resources']['memory_gib']=float('nan')
        self.assert_status('ERROR','positive')

    def test_unsupported_dependency_is_not_scientific_invalid(self):
        self.change_input('%maxcore 100','%maxcore 100\n%moinp "unknown.gbw"')
        self.assert_status('UNSUPPORTED','dependency')

    def test_scoped_blocks_and_stage_intent(self):
        self.change_input('%maxcore 100','%maxcore 100\n%scf\n MaxIter 120\nend')
        self.assertEqual(check(self.spec)['status'],'READY')
        self.spec['scientific']['profile']='opt'
        self.assert_status('ERROR','matching')

    def test_ghost_ecp_and_spin_do_not_gain_false_parity(self):
        self.change_input('H 0 0 0','Gh(H) 0 0 0')
        self.assert_status('UNSUPPORTED','parity')
        self.change_input('Gh(H)','H')
        self.change_input('xyz 0 1','xyz 0 2')
        self.assert_status('ERROR','multiplicity')

    def test_composite_method_needs_no_named_basis(self):
        self.change_input('HF STO-3G','r2SCAN-3c')
        self.assertEqual(check(self.spec)['status'],'READY')

    def test_auxiliary_basis_and_contradictory_methods_do_not_pass(self):
        self.change_input('HF STO-3G','PBE def2/J')
        self.assert_status('UNSUPPORTED','orbital basis')
        self.change_input('PBE def2/J','HF PBE STO-3G')
        self.assert_status('ERROR','Conflicting')

    def test_recorded_serial_requires_dependency_closure(self):
        self.spec['runtime']['qualification']='recorded'
        self.assert_status('UNASSESSED','runtime/MPI contract')

    def test_missing_runtime_evidence_and_changed_executable(self):
        old=self.spec['runtime'].pop('version')
        self.assert_status('UNASSESSED','Recorded')
        self.spec['runtime']['version']=old
        self.native.write_text('#!/bin/sh\nexit 0\n')
        self.assert_status('ERROR','bytes')

    def test_mpi_driver_and_parallel_without_contract_refused(self):
        target=self.root/'mpirun'; self.native.rename(target)
        self.spec['runtime']['executable']=str(target)
        self.assert_status('ERROR','mpirun')
        target.rename(self.native); self.spec['runtime']['executable']=str(self.native)
        self.change_input('nprocs 1','nprocs 2')
        self.spec['resources'].update(cpus=2,mpi_ranks=2)
        self.assert_status('UNASSESSED','Parallel')

    def test_freq_requires_explicit_policy(self):
        self.change_input('STO-3G SP','STO-3G Freq')
        self.spec['scientific']['profile']='freq'
        self.assert_status('UNASSESSED','policy')
        self.spec['scientific']['validation']={'require_minimum':False,'imaginary_tolerance_cm1':0.0}
        self.assertEqual(check(self.spec)['status'],'READY')

    def test_outputs_cannot_be_overwritten(self):
        work=Path(self.spec['workdir']); work.mkdir(); (work/'stage.out').write_text('old result')
        self.assert_status('ERROR','already exist')

    def test_preparation_preserves_original_and_hashes_generated_inputs(self):
        before=self.input.read_bytes()
        checked=check(self.spec)
        Path(self.spec['workdir']).mkdir()
        plan=prepare(self.spec,checked)
        self.assertEqual(self.input.read_bytes(),before)
        self.assertEqual(plan['progress_sources'][0]['path'],str(Path(self.spec['workdir'])/'stage.out'))
        self.assertIn('target_id',plan['scientific_association'])
        self.assertIn('geometry-input.json',{Path(item['path']).name for item in plan['dependencies']})
        with self.assertRaisesRegex(ValueError,'empty'):
            prepare(self.spec,checked)

    def test_mutated_source_after_check_is_not_prepared(self):
        checked=check(self.spec)
        self.change_input('0.74','0.75')
        Path(self.spec['workdir']).mkdir()
        with self.assertRaisesRegex(ValueError,'changed before'):
            prepare(self.spec,checked)

    def _native_output(self,valid=True):
        self.native.write_text('#!/bin/sh\n' + "printf '%s\\n' 'Program Version 6.1.1' " +
            ("'SCF CONVERGED AFTER 3 CYCLES' " if valid else "'SCF DID NOT CONVERGE' ") +
            "'FINAL SINGLE POINT ENERGY -1.1' 'ORCA TERMINATED NORMALLY'\n")
        self.spec['runtime']['sha256']=file_hash(self.native)

    def test_real_runner_validates_native_evidence_and_rejects_zero_exit_nonconvergence(self):
        for valid in (True,False):
            with self.subTest(valid=valid):
                self.spec['workdir']=str(self.root/('valid' if valid else 'invalid'))
                self._native_output(valid)
                checked=check(self.spec)
                self.assertEqual(checked['status'],'READY',checked)
                Path(self.spec['workdir']).mkdir()
                plan=prepare(self.spec,checked)
                result=subprocess.run(plan['argv'],cwd=plan['cwd'],env={**os.environ,**plan['env']},capture_output=True,text=True,timeout=20)
                self.assertEqual(result.returncode,0 if valid else 70,result.stdout+result.stderr)
                record=json.loads((Path(plan['cwd'])/'job.json').read_text())
                self.assertEqual(record['scientific']['status'],'VALID' if valid else 'NOT_CONVERGED')

    def test_prepared_workflow_uses_exact_state_and_keeps_stage_gates(self):
        geometry=self.root/'source.xyz';geometry.write_text('2\nH2\nH 0 0 0\nH 0 0 0.74\n')
        config=self.root/'workflow.json';config.write_text(json.dumps({'schema_version':1,'charge':0,'multiplicity':1,
            'resources':{'nprocs':1,'maxcore_mb_per_process':100},
            'validation':{'require_minimum':False,'imaginary_tolerance_cm1':0},
            'stages':{stage:{'keywords':'HF STO-3G','blocks':[]} for stage in ('opt','freq','sp')}}))
        self.spec['scientific']={'profile':'opt+freq+sp','workflow':True,'structure':str(geometry),'config':str(config)}
        checked=check(self.spec);self.assertEqual(checked['status'],'READY',checked)
        Path(self.spec['workdir']).mkdir()
        plan=prepare(self.spec,checked)
        self.assertEqual(plan['progress_sources'],[])
        locations=orca_workflow_state_locations({'argv':plan['argv'],'cwd':plan['cwd']})
        sources=[item for item in locations if item.role=='orca-output' and item.path]
        self.assertEqual(len(sources),1)
        self.assertEqual(sources[0].stage,'OPT')
        self.assertNotEqual(sources[0].path,str(Path(plan['cwd'])/'stage.out'))
        self.assertIn(plan['scientific_association']['workflow_state'],{item['path'] for item in plan['dependencies']})


    def test_jobs_owned_sp_and_opt_lifecycle(self):
        from tests.molecular.orca.jobs_fixture import owned_jobs_surface
        report=owned_jobs_surface(self.root/'owned')
        self.assertEqual(set(report),{'sp','opt'})

    def test_real_composition_keeps_freq_failure_gate(self):
        from tests.molecular.orca.jobs_fixture import make_spec
        for failed in (False,True):
            with self.subTest(failed=failed):
                spec=make_spec(self.root/('workflow-fail' if failed else 'workflow-ok'),
                               profile='opt+freq+sp',workflow=True,fail_stage='FREQ' if failed else None)
                checked=check(spec);self.assertEqual(checked['status'],'READY',checked)
                Path(spec['workdir']).mkdir()
                plan=prepare(spec,checked)
                result=subprocess.run(plan['argv'],cwd=plan['cwd'],env={**os.environ,**plan['env']},capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,70 if failed else 0,result.stdout+result.stderr)
                state=json.loads(Path(plan['scientific_association']['workflow_state']).read_text())
                self.assertEqual(state['stages']['OPT']['status'],'VALID')
                self.assertEqual(state['stages']['FREQ']['status'],'FAILED' if failed else 'VALID')
                self.assertEqual(state['stages']['SP']['status'],'PENDING' if failed else 'VALID')


class RecordedRuntimeTests(unittest.TestCase):
    def test_static_runtime_checks_helper_and_platform_without_probes(self):
        import platform
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve()
            paths={name:root/name for name in ('orca','mpirun','ompi_info','orca_startup_mpi')}
            for name,path in paths.items():path.write_text(name);path.chmod(0o755)
            helpers={str(paths['orca_startup_mpi']):{'size_bytes':paths['orca_startup_mpi'].stat().st_size,'sha256':file_hash(paths['orca_startup_mpi'])}}
            record={'schema_version':3,'platform':platform.system(),'architecture':platform.machine(),'nprocs':2,
                'orca':{'executable':str(paths['orca']),'sha256':file_hash(paths['orca'])},
                'mpi':{'launcher':str(paths['mpirun']),'launcher_sha256':file_hash(paths['mpirun']),
                    'capabilities':{'inspector':str(paths['ompi_info']),'inspector_sha256':file_hash(paths['ompi_info'])}},
                'dependency_validation':{'helpers_inspected':list(helpers),'helper_artifacts':helpers,'library_artifacts':{}}}
            record['runtime_id']=stable_hash(record);record['validation']={'status':'PASSED'}
            with patch('subprocess.run',side_effect=AssertionError('probe')):
                self.assertEqual(len(inspect_recorded_orca_runtime(record,orca_executable=paths['orca'],nprocs=2)),4)
            paths['orca_startup_mpi'].write_text('changed helper')
            with self.assertRaises(OrcaRuntimeError):
                inspect_recorded_orca_runtime(record,orca_executable=paths['orca'],nprocs=2)
