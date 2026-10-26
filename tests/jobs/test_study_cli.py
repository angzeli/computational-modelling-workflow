"""Core-only study command grammar and explicit owner path forwarding."""
import argparse
from contextlib import redirect_stderr,redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
from cmw.jobs.cli import register
from cmw.jobs.store import Store
from cmw.jobs.study_models import StudyResult


class StudyCliTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='cmw-study-cli-');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.store=Store(self.root/'state')
        self.result=StudyResult(1,uuid4().hex,uuid4().hex,uuid4().hex,'plan','READY','a'*64,'{"cases":[]}')

    def call(self,arguments,*,local=True):
        parser=argparse.ArgumentParser();register(parser.add_subparsers());output=io.StringIO()
        with redirect_stdout(output),redirect_stderr(output):
            try:
                args=parser.parse_args(['jobs',*(['--state',str(self.store.root)] if local else []),*arguments])
                code=args.handler(args)
            except SystemExit as exc:code=exc.code
        return code,output.getvalue()

    def test_plan_is_complete_json_and_creates_no_state(self):
        with patch('cmw.jobs.study.StudyService') as factory:
            factory.return_value.plan.return_value=self.result
            code,output=self.call(['study','plan','--manifest','/owner/m','--json'])
            self.assertEqual(code,0,output);self.assertEqual(json.loads(output)['payload'],{'cases':[]})
            factory.return_value.plan.assert_called_once_with('/owner/m')
            factory.return_value.materialize.assert_not_called()
        self.assertFalse(self.store.root.exists())

    def test_separate_publication_registration_compare_export_and_view(self):
        with patch('cmw.jobs.study.StudyService') as factory:
            service=factory.return_value
            for action,status in (('materialize','MATERIALIZED'),('register','REGISTERED'),('compare','COMPLETE'),('view','COMPLETE')):
                getattr(service,action).return_value=replace(self.result,operation=action,status=status)
            for arguments in (
                ['materialize','--manifest','/owner/m','--approve-digest','a'*64],
                ['register','--record','/owner/r','--approve-digest','a'*64,'--request-id',self.result.study_id],
                ['compare','--record','/owner/r','--selection','/owner/s','--export','/owner/new'],
                ['view','--record','/owner/r']):
                code,output=self.call(['study',*arguments,'--json']);self.assertEqual(code,0,output)
            service.materialize.assert_called_once_with('/owner/m','a'*64)
            service.register.assert_called_once_with('/owner/r','a'*64,self.result.study_id)
            service.compare.assert_called_once_with('/owner/r','/owner/s',export_directory='/owner/new')
            service.view.assert_called_once_with('/owner/r')

    def test_required_approval_selection_request_and_compact_view(self):
        for arguments in (['materialize','--manifest','/m'],['register','--record','/r','--approve-digest','a'*64],['compare','--record','/r']):
            self.assertEqual(self.call(['study',*arguments])[0],2)
        with patch('cmw.jobs.study.StudyService') as factory:
            factory.return_value.view.return_value=replace(self.result,operation='view',status='NOT_BOUND')
            code,output=self.call(['study','view','--record','/r'])
            self.assertEqual(code,2);self.assertIn('No bound study cases',output)

    def test_remote_paths_are_never_opened_on_frontend(self):
        from tests.nodes.test_study import StudyNodeTests
        fixture=StudyNodeTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        with patch('cmw.nodes.jobs_cli.connect',return_value=fixture.client()):
            code,output=self.call(['--node','fixture','study','compare','--record','/owner/absent/r',
                '--selection','/owner/absent/s','--json'],local=False)
        self.assertEqual(code,0,output)
        fixture.service.compare.assert_called_once_with('/owner/absent/r','/owner/absent/s',export_directory=None)

    def test_cli_remains_independent_of_textual(self):
        script=('import argparse,sys;from cmw.jobs.cli import register;p=argparse.ArgumentParser();'
            'register(p.add_subparsers());p.parse_args(["jobs","study","view","--record","/r"]);'
            'from cmw.jobs.study_presentation import study_text;assert "textual" not in sys.modules')
        result=subprocess.run([sys.executable,'-B','-c',script],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
