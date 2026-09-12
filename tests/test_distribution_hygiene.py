from pathlib import Path
import io
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tests.distribution_hygiene import REDUCED_LOGS, artifact_findings, findings, source_findings


class DistributionHygieneTests(unittest.TestCase):
    def test_tracked_publication_inputs_have_no_private_paths_or_runtime_state(self):
        self.assertEqual(source_findings(Path(__file__).resolve().parents[1]), [])

    def test_home_paths_rejected_and_neutral_fixture_paths_allowed(self):
        for home in ('Users', 'home'):
            data = ('/'+home+'/example/project/input.xyz').encode()
            self.assertTrue(findings('provenance.json', data))
        self.assertEqual(findings('provenance.json', b'/fixture-root/pdi/input.xyz'), [])
        self.assertTrue(findings('provenance.json', ('/var/'+'folders/xx/machine/T/input').encode()))
        self.assertTrue(findings(next(iter(REDUCED_LOGS)), data))

    def test_runtime_artifacts_rejected_and_reduced_parser_log_allowed(self):
        for name in ('POTCAR', 'WAVECAR', 'run.gbw', 'jobs.sqlite3', 'controller.log',
                     '__pycache__/module.pyc', 'runs/result.json', 'vasp_std', 'vasp_gam',
                     'vasp_ncl', 'orca', 'Multiwfn'):
            self.assertTrue(findings(name, b''), name)
        self.assertEqual(findings(next(iter(REDUCED_LOGS)), b'scientific parser excerpt'), [])

    def test_archive_content_is_checked_without_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = ('/'+ 'Users/example/source.xyz').encode()
            wheel = root/'example.whl'
            with zipfile.ZipFile(wheel, 'w') as archive:
                archive.writestr('cmw/provenance.json', content)
            sdist = root/'example.tar.gz'
            with tarfile.open(sdist, 'w:gz') as archive:
                member = tarfile.TarInfo('example-0.1.0/provenance.json')
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
            for artifact in (wheel, sdist):
                self.assertTrue(any('local-home path' in finding
                                    for finding in artifact_findings(artifact)))
            self.assertEqual({path.name for path in root.iterdir()}, {wheel.name, sdist.name})

    def test_tracked_symlink_is_refused_without_reading_its_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'outside').symlink_to(root/'missing-target')
            with patch('tests.distribution_hygiene.subprocess.check_output', return_value=b'outside\0'):
                self.assertEqual(source_findings(root), ['outside: non-regular tracked input'])
