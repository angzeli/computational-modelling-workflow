from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cmw.jobs.suspension_protocol import SuspensionProtocolError
from cmw.molecular.orca.suspension import describe

ROOT = Path(__file__).parents[2]


class OrcaSuspensionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cmw orca suspension ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.structure = self.root / 'geometry.xyz'
        shutil.copyfile(ROOT / 'tests/fixtures/xyz/synthetic.xyz', self.structure)
        self.input = self.root / 'stage.inp'
        self.target = self.root / 'target.json'
        self.fake = ROOT / 'tests/fixtures/fake_orca.py'
        self.environment = {**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1',
                            'PYTHON_BIN': sys.executable, 'CMW_CAFFEINATE': '0', 'ORCA_EXE': str(self.fake)}
        self.environment.pop('CMW_SUSPENSION_CONTEXT', None)

    def prepare(self, stage='SP', keywords='HF STO-3G', nprocs=1, blocks=()):
        command = [sys.executable, '-m', 'cmw.molecular.orca.cli', 'prepare', '--stage', stage,
                   '--structure', str(self.structure), '--charge', '0', '--multiplicity', '1',
                   '--keywords', keywords, '--nprocs', str(nprocs), '--maxcore', '64',
                   '--input', str(self.input), '--target', str(self.target)]
        for block in blocks:
            command.extend(['--block', block])
        subprocess.run(command, env=self.environment, check=True, capture_output=True, text=True)

    def test_serial_sp_and_opt_readiness_does_not_launch_engine(self):
        for stage in ('SP', 'OPT'):
            self.prepare(stage, keywords='PBE0 def2-SVP')
            with patch.dict(os.environ, {'CMW_CAFFEINATE': '0'}):
                result = describe(self.input, self.target, nprocs=1, orca_exe=self.fake)
            self.assertEqual(result['stage'], stage)
            self.assertEqual(result['ranks'], 1)
            self.assertFalse(result['native_engine_qualified'])
            self.assertFalse(result['sleep_qualified'])
            self.assertTrue(result['local_only'])

    def test_unsupported_frequency_unknown_keyword_and_blocks_refused(self):
        for stage, keywords, blocks in [('FREQ', 'HF STO-3G', ()),
                                       ('SP', 'HF STO-3G NumFreq', ()),
                                       ('OPT', 'HF STO-3G', ('%geom MaxIter 10 end',))]:
            self.prepare(stage, keywords, blocks=blocks)
            with self.assertRaises(SuspensionProtocolError):
                describe(self.input, self.target, nprocs=1, orca_exe=self.fake)

    def test_resources_and_scientific_target_must_match(self):
        self.prepare(nprocs=2)
        with self.assertRaisesRegex(SuspensionProtocolError, 'PAL count'):
            describe(self.input, self.target, nprocs=1, orca_exe=self.fake)
        self.input.write_text(self.input.read_text().replace('HF STO-3G', 'PBE STO-3G'))
        with self.assertRaisesRegex(SuspensionProtocolError, 'scientific target'):
            describe(self.input, self.target, nprocs=2, orca_exe=self.fake)

    def test_parallel_route_requires_exact_runtime_and_no_power_assertion(self):
        self.prepare(nprocs=2)
        with self.assertRaisesRegex(SuspensionProtocolError, 'runtime contract'):
            describe(self.input, self.target, nprocs=2, orca_exe=self.fake)
        with patch.dict(os.environ, {'CMW_CAFFEINATE': '1'}):
            with self.assertRaisesRegex(SuspensionProtocolError, 'power assertions'):
                describe(self.input, self.target, nprocs=2, orca_exe=self.fake)

    def test_explicit_shell_enrollment_without_worker_fails_before_engine_or_output(self):
        self.prepare()
        output = self.root / 'stage.out'
        metadata = self.root / 'attempt.json'
        result = subprocess.run([str(ROOT / 'scripts/orca/run_orca.sh'), '--input', str(self.input),
            '--target', str(self.target), '--metadata', str(metadata), '--output', str(output),
            '--stderr', str(self.root / 'stage.err'), '--controlled-suspension'],
            env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 69, result.stderr)
        self.assertIn('newly enrolled', result.stderr)
        self.assertFalse(output.exists())
        self.assertFalse(metadata.exists())
        self.assertFalse(Path(str(self.target) + '.lock').exists())
