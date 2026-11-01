"""The shell bridge decodes one response without evaluating or splitting fields."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from tests.workflows.test_opt_freq_sp import RUNNER


class ActionBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmw action bridge ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root/'state.json'
        self.state.write_text('{}')
        self.scratch = self.root/'tmp'
        self.scratch.mkdir()
        self.reply = self.root/'reply.json'
        self.phase = self.root/'advanced'
        self.calls = self.root/'decodes'
        self.arguments = self.root/'arguments'
        self.resources = self.root/'resources'
        self.marker = self.root/'must-not-exist'
        literal = f'雪 space "quote" \\slash\nline $(touch {self.marker})'
        self.action = {'action': 'RUN', 'stage': 'opt', 'nprocs': 2,
            'maxcore_mb_per_process': 128, 'require_minimum': False,
            'imaginary_tolerance_cm1': 5.5}
        for key in ('input', 'target', 'metadata', 'output', 'stderr',
                    'final_geometry', 'layout', 'geometry_contract', 'orca_exe'):
            self.action[key] = str(self.root/(key+' '+literal))
        quote = shlex.quote
        self.python = self.root/'selected python'
        self.python.write_text(f'''#!/bin/bash
if [[ "$1" == -m ]]; then
  if [[ "$3" == next ]]; then
    if [[ -e {quote(str(self.phase))} ]]; then
      printf '%s' '{{"action":"COMPLETE","result_path":"result.json"}}'
      exit 10
    fi
    cat {quote(str(self.reply))}
    exit "${{BRIDGE_NEXT_STATUS:-0}}"
  fi
  if [[ "$3" == advance ]]; then
    touch {quote(str(self.phase))}
    exit 0
  fi
  exit 90
fi
printf '%s\\n' decode >> {quote(str(self.calls))}
if [[ "${{BRIDGE_DECODE:-}}" == truncated ]]; then
  printf 'RUN\\0'
  exit 0
fi
if [[ "${{BRIDGE_DECODE:-}}" == failed ]]; then
  {quote(sys.executable)} "$@"
  exit 9
fi
exec {quote(sys.executable)} "$@"
''')
        self.python.chmod(0o700)
        self.runner = self.root/'fake runner'
        self.runner.write_text(f'''#!/bin/bash
printf '%s\\0' "$@" > {quote(str(self.arguments))}
printf '%s\\0' "$ORCA_EXE" "$NPROCS" "$MAXCORE_MB" "$CMW_REQUIRE_MINIMUM" "$CMW_IMAGINARY_TOLERANCE_CM1" > {quote(str(self.resources))}
''')
        self.runner.chmod(0o700)

    def run_bridge(self, *, reply=None, **environment):
        self.reply.write_text(json.dumps(self.action) if reply is None else reply)
        result = subprocess.run(['/bin/bash', str(RUNNER), '--state', str(self.state)],
            env={**os.environ, 'PYTHON_BIN': str(self.python),
                 'CMW_ORCA_RUNNER': str(self.runner), 'TMPDIR': str(self.scratch),
                 **environment}, capture_output=True, text=True, timeout=5)
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertFalse(self.marker.exists())
        return result

    def test_one_decode_per_action_preserves_literal_fields(self):
        result = self.run_bridge()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.arguments.read_bytes().decode().split('\0')[:-1], [
            '--input', self.action['input'], '--target', self.action['target'],
            '--metadata', self.action['metadata'], '--output', self.action['output'],
            '--stderr', self.action['stderr'], '--layout', self.action['layout'],
            '--geometry-contract', self.action['geometry_contract'],
            '--artifact', 'final_geometry='+self.action['final_geometry']])
        self.assertEqual(self.resources.read_bytes().decode().split('\0')[:-1],
            [self.action['orca_exe'], '2', '128', '0', '5.5'])
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_invalid_decoder_output_never_launches(self):
        for mode in ('failed', 'truncated'):
            with self.subTest(mode=mode):
                result = self.run_bridge(BRIDGE_DECODE=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.arguments.exists())

    def test_invalid_json_object_or_nul_never_launches(self):
        for reply in ('{', '[]', json.dumps({**self.action, 'input': 'bad\0path'})):
            with self.subTest(reply=reply):
                result = self.run_bridge(reply=reply)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.arguments.exists())

    def test_action_and_exit_status_must_agree(self):
        for action, status, expected in [('RUN', '20', 70), ('COMPLETE', '0', 64),
                                          ('FAILED', '0', 70), ('UNKNOWN', '0', 64)]:
            with self.subTest(action=action, status=status):
                result = self.run_bridge(reply=json.dumps({'action': action}),
                                         BRIDGE_NEXT_STATUS=status)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertFalse(self.arguments.exists())
