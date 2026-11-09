"""CI routing must retain coverage while avoiding duplicate ready-event Ubuntu runs."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts/ci' / (name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


planner, docs = load('plan'), load('check_docs')
POLICY = json.loads(planner.POLICY.read_text())


class TierTests(unittest.TestCase):
    def select(self, paths, *, action='synchronize', draft=True, label='', event='pull_request'):
        return planner.plan(event, {'action': action, 'label': {'name': label},
                            'pull_request': {'draft': draft, 'labels': []}}, paths, POLICY)

    def test_draft_fast_tier(self):
        result = self.select(['src/cmw/structure/cif.py'])
        self.assertEqual(result['suites'], planner.PLATFORMS[:1])
        self.assertEqual(result['artifacts'], [{**planner.PLATFORMS[0], 'modes': 'core'}])
        self.assertEqual(result['sweep'], [])

    def test_ready_and_label_never_duplicate_ubuntu(self):
        for action, label in [('ready_for_review', ''), ('labeled', 'ci:merge')]:
            with self.subTest(action=action):
                result = self.select(['src/cmw/jobs/ownership.py'], action=action, label=label)
                self.assertEqual(result['suites'], planner.PLATFORMS[1:])
                self.assertEqual(result['artifacts'], [])
                self.assertEqual(result['sweep'], [])

    def test_non_draft_open_and_new_push_refresh_merge_tier(self):
        for action in ('opened', 'synchronize', 'reopened'):
            self.assertEqual(self.select(['src/cmw/cli_helpers.py'], action=action, draft=False)['suites'], planner.PLATFORMS)

    def test_merge_label_on_draft_survives_push(self):
        event = {'action': 'synchronize', 'pull_request': {'draft': True, 'labels': [{'name': 'ci:merge'}]}}
        self.assertEqual(planner.plan('pull_request', event, ['src/cmw/structure/cif.py'], POLICY)['suites'], planner.PLATFORMS)

    def test_docs_only_skips_suites_even_ready(self):
        for action in ('synchronize', 'ready_for_review'):
            result = self.select(['docs/architecture/multi-host-nodes.md', 'AGENTS.md', 'docs/website/index.html'], action=action)
            self.assertTrue(result['docs_only'])
            self.assertTrue(result['active'])
            for key in ('suites', 'artifacts', 'sweep'): self.assertEqual(result[key], [])

    def test_contract_json_is_not_prose(self):
        self.assertFalse(self.select(['docs/schemas/request.json'])['docs_only'])
        self.assertEqual(len(self.select(['docs/schemas/request.json'])['suites']), 1)

    def test_every_requested_critical_family_and_tests_select_both_matrices(self):
        paths = ['src/cmw/jobs/'+name for name in ('ownership.py', 'ownership_hold.py', 'runtime.py',
                 'suspension.py', 'suspension_protocol.py', 'process_identity.py', 'payload.sh', 'store.py')]
        paths += ['src/cmw/nodes/identity.py', 'src/cmw/molecular/orca/suspension.py',
                  'tests/jobs/test_historical_vasp_runner.py', 'tests/jobs/ownership_read_manifest.py',
                  'tests/jobs/test_ownership_fault_sweep.py', 'tests/fixtures/vasp_port_51d8209/launch.py']
        for path in paths:
            with self.subTest(path=path): self.assertEqual(self.select([path])['sweep'], planner.PLATFORMS)

    def test_packaging_and_packaged_assets_select_all_modes_both_platforms(self):
        for path in ('pyproject.toml', 'uv.lock', 'MANIFEST.in', 'setup.cfg', 'hatch.toml',
                     'scripts/cmw', 'src/cmw/cli.py', 'scripts/multiwfn/run_ifct.sh'):
            with self.subTest(path=path):
                self.assertEqual(self.select([path])['artifacts'], [{**p, 'modes': 'all'} for p in planner.PLATFORMS])

    def test_ci_change_full_label_main_and_nightly_select_everything(self):
        cases = [self.select(['.github/workflows/python-package.yml']), self.select(['scripts/ci/paths.json']),
                 self.select(['tests/test_ci_tiers.py']), self.select(['README.md'], action='labeled', label='ci:full'),
                 self.select([], event='push'), self.select([], event='schedule')]
        for result in cases:
            self.assertTrue(result['full'])
            self.assertEqual(result['suites'], planner.PLATFORMS)
            self.assertEqual(result['sweep'], planner.PLATFORMS)
            self.assertEqual(result['artifacts'], [{**p, 'modes': 'all'} for p in planner.PLATFORMS])

    def test_full_label_persists_on_new_commits(self):
        event = {'action': 'synchronize', 'pull_request': {'draft': True, 'labels': [{'name': 'ci:full'}]}}
        result = planner.plan('pull_request', event, ['README.md'], POLICY)
        self.assertTrue(result['full'])
        self.assertEqual(result['suites'], planner.PLATFORMS)

    def test_workflow_wires_all_plans_and_preserves_safety_runners(self):
        import yaml
        workflow = yaml.load((ROOT/'.github/workflows/python-package.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertNotIn('workflow_dispatch', workflow['on'])
        self.assertIn('schedule', workflow['on'])
        self.assertEqual(workflow['on']['push']['branches'], ['main'])
        self.assertIn('ready_for_review', workflow['on']['pull_request']['types'])
        jobs = workflow['jobs']
        for job, key in [('tests', 'suites'), ('ownership-sweep', 'sweep'), ('artifacts', 'artifacts')]:
            self.assertEqual(jobs[job]['strategy']['matrix']['include'], '${{ fromJSON(needs.plan.outputs.'+key+') }}')
            self.assertEqual(jobs[job]['if'], "needs.plan.outputs."+key+" != '[]'")
        self.assertEqual(jobs['tests']['env']['CMW_REQUIRE_SFTP_INTEGRATION'], '1')
        suite = next(s for s in jobs['tests']['steps'] if s.get('name') == 'Offline suite')
        self.assertEqual(suite['env']['CMW_FAULT_SWEEP'], 'representative')

    def test_empty_diff_and_mixed_diff_never_get_docs_exemption(self):
        for paths in ([], ['README.md', 'src/cmw/new.py']):
            self.assertFalse(self.select(paths)['docs_only'])
            self.assertTrue(self.select(paths)['suites'])

    def test_unrelated_label_does_not_launch_tests(self):
        result = self.select(['src/cmw/jobs/runtime.py'], action='labeled', label='discussion')
        self.assertFalse(result['active'])
        self.assertEqual(result['suites'], [])
        self.assertEqual(result['sweep'], [])

    def test_invalid_sha_refuses_diff_without_running_git(self):
        with self.assertRaises(ValueError):
            planner.changed_paths('pull_request', {'pull_request': {'base': {'sha': '--evil'}, 'head': {'sha': 'a'*40}}})

    def test_existing_runners_keep_every_module_and_full_union(self):
        source = (ROOT/'scripts/ci/run_tests.sh').read_text()
        self.assertIn("find \"$source_root/tests\" -name 'test_*.py'", source)
        sweep = (ROOT/'scripts/ci/run_ownership_sweep.sh').read_text()
        self.assertIn('CMW_FAULT_SWEEP=full', sweep)
        self.assertIn('check_ownership_sweep.py', sweep)
        self.assertGreater(len(list((ROOT/'tests').rglob('test_*.py'))), 300)


class DocumentationChecks(unittest.TestCase):
    def test_missing_link_and_unclosed_fence_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root/'readme.md'
            path.write_text('[broken](missing.md)\n```sh\necho test\n')
            errors = docs.markdown_errors(path, root)
            self.assertEqual(len(errors), 2)
            self.assertTrue(any('missing local link' in e for e in errors))
            self.assertTrue(any('unclosed code fence' in e for e in errors))

    def test_code_examples_external_links_and_existing_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root/'readme.md'; (root/'target.md').touch()
            path.write_text('[ok](target.md#heading) [web](https://example.com)\n```md\n[example](missing.md)\n```\n')
            self.assertEqual(docs.markdown_errors(path, root), [])
