"""Cold stock launcher and payload acceptance in a disposable installed wheel.

The driver uses -I -B. The tested console scripts use their installed shebang,
a private HOME, and no inherited Python settings. Only process discovery is
restricted, to avoid inspecting unrelated work or depending on machine load.
"""
import argparse
import compileall
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


def installed_stock_surface(output):
    output = Path(output)
    child = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).resolve()),
        '--owned-prefix', sys.prefix, '--output', str(output)],
        capture_output=True, text=True, timeout=180)
    if child.returncode:
        raise AssertionError('Stock installed launcher failed:\n' + child.stdout[-4000:] + '\n' + child.stderr[-8000:])
    return json.loads(output.read_text())


@contextmanager
def cold_installation(package):
    # Save the installer's original caches. Scope the census in children before
    # any CMW import; importing CMW in this hook would mask stock startup.
    site = package.parent
    with tempfile.TemporaryDirectory(prefix='stock-caches-', dir=site) as temporary:
        saved = {}
        try:
            for i, path in enumerate(sorted(package.rglob('__pycache__'))):
                assert path.resolve(strict=True) == path and path.is_dir()
                saved[path] = path.rename(Path(temporary) / str(i))
            hook = site / '_cmw_stock_test_census.pth'
            with hook.open('x') as stream:
                # Ownership uses the real process group to prove payload drainage.
                stream.write("import psutil, sys; psutil.pids = lambda pids=psutil.pids, frame=sys._getframe: "
                             "[] if frame(1).f_globals.get('__name__') == 'cmw.jobs.activity' else pids()\n")
            try:
                yield
            finally:
                hook.unlink()
                for path in list(package.rglob('__pycache__')):
                    shutil.rmtree(path)
        finally:
            for path, backup in saved.items():
                backup.rename(path)


class StockLauncherTests(unittest.TestCase):
    package = output = None
    evidence = []

    def setUp(self):
        from cmw.jobs.store import Store
        self.enterContext(cold_installation(self.package))
        self.directory = self.output.parent / self._testMethodName
        self.directory.mkdir(mode=0o700)
        storage = self.directory / 'storage'
        storage.mkdir()
        self.config = self.directory / 'node'
        self.queue = self.directory / 'queue'
        self.store = Store(self.queue)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(('PYTHON', 'CMW_'))}
        self.env['HOME'] = str(self.directory)
        self.cli = str(Path(sys.prefix) / 'bin/cmw')

    def enroll(self):
        from cmw.nodes.identity import enroll
        return enroll(self.config, {'queues': {'main': str(self.queue)},
            'storages': {'scratch': str(self.directory / 'storage')},
            'profiles': {'echo': {'engine': 'Command', 'runner': '/bin/echo', 'max_cpus': 1}}})

    def invoke(self, *args):
        child = subprocess.run([self.cli, *args], env=self.env, cwd=self.directory,
                               capture_output=True, text=True, timeout=35)
        self.assertEqual(child.returncode, 0, child.stdout[-2400:] + child.stderr[-2400:])
        return json.loads(child.stdout)

    def caches(self):
        return {path.relative_to(self.package): (s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
                for path in self.package.rglob('*.pyc') for s in [path.stat()]}

    def wait_for(self, predicate):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.05)
        self.fail('Owned queue timed out: ' + str(self.store.snapshot()['jobs'])[-2400:])

    def stop(self):
        from cmw.jobs import runtime
        runtime.stop(self.store)
        self.wait_for(lambda: not self.store.snapshot()['controller']['online'] and
                      not runtime.lock_held(self.queue / 'controller.lock'))

    def test_stock_source_status_across_jobs_and_controller_restart(self):
        from cmw.nodes.transport import ProcessTransport
        self.assertEqual(self.caches(), {})
        self.enroll()
        self.invoke('jobs', '--state', self.queue, 'start', '--json')
        self.addCleanup(self.stop)
        original = self.store.snapshot()['controller']['owner']
        endpoint = ProcessTransport([self.cli, 'nodes', 'endpoint', '--config', str(self.config),
                                     '--queue', 'main'], env=self.env, timeout=35)
        self.addCleanup(endpoint.close)
        endpoint.exchange('capabilities')
        sequences = []

        def status():
            response = endpoint.exchange('status')
            self.assertTrue(response['result']['state']['controller']['online'])
            self.assertEqual(self.caches(), {})
            sequences.append(response['sequence'])

        status()  # First stock status lazily imports thermal_config, telemetry and thermal.
        status()
        for index, override in enumerate(('', '0')):
            receipt = self.directory / f'helper-{index}.json'
            job = self.store.add(argv=[sys.executable, '-m', 'cmw.jobs.runtime', 'receipt', str(receipt), '7'],
                cwd=str(self.directory), env={'PYTHONDONTWRITEBYTECODE': override},
                name=f'stock helper {index}', cpus=1, memory_gib=.1)
            self.wait_for(lambda: any(row['id'] == job['id'] and row['status'] == 'Done'
                                      for row in self.store.snapshot()['jobs']))
            self.assertEqual(json.loads(receipt.read_text())['exit_code'], 7)
            written = self.queue / 'attempts' / job['attempt_id'] / 'payload-exit.json'
            self.assertEqual(json.loads(written.read_text())['exit_code'], 0)
            status()
        self.stop()
        self.invoke('jobs', '--state', self.queue, 'start', '--json')
        self.assertNotEqual(self.store.snapshot()['controller']['owner'], original)
        status()
        self.assertEqual(len(set(sequences)), 5)
        self.evidence.append({'case': 'stock-source', 'statuses_passed': len(sequences),
            'new_caches': 0, 'controller_restarted': True, 'job_overrides': ['', '0']})

    def test_stock_precompiled_optimized_status_through_local_registry(self):
        from cmw.nodes.connections import save_connection
        self.assertEqual(self.caches(), {})
        self.assertTrue(compileall.compile_dir(self.package, quiet=2, optimize=0))
        before = self.caches()
        self.assertTrue(before)
        self.assertFalse(any('.opt-' in path.name for path in before))
        registry = self.directory / 'frontend'
        config = self.enroll()
        save_connection(registry, 'fixture', {'transport': 'local', 'config_root': str(self.config),
            'node_id': config['node_id'], 'host_id': config['host_id'],
            'runtime_id': config['runtime']['fingerprint'], 'default_queue': 'main',
            'queues': {'main': config['queues']['main']['queue_id']}, 'read_only': True})
        self.env['PYTHONOPTIMIZE'] = '1'
        self.invoke('nodes', 'check', 'fixture', '--config', registry, '--json')
        for _ in range(5):
            reply = self.invoke('jobs', '--node', 'fixture', '--nodes-config', registry, 'status', '--json')
            self.assertEqual(reply['node_id'], config['node_id'])
            self.assertEqual(reply['queue_id'], config['queues']['main']['queue_id'])
            self.assertEqual(self.caches(), before)
        self.evidence.append({'case': 'stock-optimized-wheel', 'statuses_passed': 5,
                              'level_zero_caches': len(before), 'new_caches': 0})


def main():
    # -I excludes the script directory; add only the copied test helper directory.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from bytecode_acceptance import owned_package
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owned-prefix', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    StockLauncherTests.package = owned_package(args.owned_prefix)
    StockLauncherTests.output = args.output
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(StockLauncherTests))
    args.output.write_text(json.dumps({'tests_run': result.testsRun, 'failures': len(result.failures),
        'errors': len(result.errors), 'cases': StockLauncherTests.evidence}, indent=2) + '\n')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
