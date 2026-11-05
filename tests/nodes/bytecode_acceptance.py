"""Request-boundary acceptance against a real, owned, precompiled installation.

This helper runs in an isolated -B subprocess, outside the source checkout.
Only cache files inside the explicitly selected virtual environment are changed;
every injected change is restored before the next case. No node is enrolled and
no queue, process discovery, thermal provider or scientific runner is used.
"""
from __future__ import annotations

import argparse
import compileall
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


def installed_bytecode_surface(output, *, kind):
    """Run the installed fixture; editable source trees are explicitly excluded."""
    if kind == 'editable':
        return {'status': 'SKIPPED', 'tests_run': 0,
                'reason': 'This acceptance requires an owned non-editable installation'}
    output = Path(output)
    child = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).resolve()),
        '--owned-prefix', sys.prefix, '--output', str(output)],
        capture_output=True, text=True, timeout=180)
    if child.returncode:
        raise AssertionError('Installed bytecode acceptance failed:\n' +
                             child.stdout[-8000:] + '\n' + child.stderr[-8000:])
    return json.loads(output.read_text())


def prepare_installation(owned_prefix):
    """Refuse source/editable/global installs before compiling or changing bytes."""
    prefix = Path(sys.prefix).resolve(strict=True)
    assert Path(owned_prefix).resolve(strict=True) == prefix, 'Wrong owning environment'
    assert prefix != Path(sys.base_prefix).resolve(), 'A disposable virtual environment is required'
    assert (prefix / 'pyvenv.cfg').is_file(), 'Missing virtual environment ownership boundary'
    assert prefix.stat().st_uid == os.getuid(), 'Virtual environment is not owned by this user'
    assert sys.flags.isolated and sys.dont_write_bytecode, 'Use this helper with -I -B'
    spec = importlib.util.find_spec('cmw')
    assert spec is not None and spec.origin is not None, 'CMW is not installed'
    origin = Path(spec.origin).absolute()
    assert origin == origin.resolve(strict=True), 'Installed package must not be a source symlink'
    assert origin.is_relative_to(prefix) and 'site-packages' in origin.parts, origin
    root = origin.parent
    sources = sorted(root.rglob('*.py'))
    assert 0 < len(sources) <= 4096, 'Unexpected installed Python source inventory'
    for source in sources:
        assert source == source.resolve(strict=True) and source.is_relative_to(root), source
        assert compileall.compile_file(str(source), quiet=2), source
    # Import only after explicit precompilation: -B prevents writes, not reads.
    import cmw
    from cmw.nodes import identity
    from cmw.nodes import runtime_observation
    cache = Path(cmw.__cached__).absolute()
    assert cache.is_file() and cache == cache.resolve(strict=True)
    assert cache.is_relative_to(root) and '__pycache__' in cache.parts
    assert cache.stat().st_uid == os.getuid() and cache.stat().st_nlink == 1
    return root, cache, identity, runtime_observation, len(sources)


class InstalledBytecodeBoundaryTests(unittest.TestCase):
    root = cache = identity = runtime = None
    evidence = []

    def begin(self):
        record, observed = self.identity.runtime_observed()
        verifier = self.runtime.RuntimeVerifier(record, observed)
        request = {}
        self.assertEqual(verifier.verify_request(self.identity.runtime_observed,
            self.identity.runtime_boundary_observed, request), record)
        self.assertIn(str(self.cache.relative_to(self.root)), request['snapshot']['auxiliary']['members'])
        self.assertNotIn('auxiliary', observed)
        return verifier, request, record

    def full_boundary(self, snapshot):
        """Reference: repeat the full content scan, using the same comparison policy."""
        captured = {}
        record, observed = self.identity.runtime_observed(request_boundary=captured)
        return record, observed, captured['directory_epochs'], captured['auxiliary']

    def finish(self, verifier, request, strategy):
        provider = self.identity.runtime_boundary_observed if strategy == 'cheap' else self.full_boundary
        return verifier.verify_request(self.identity.runtime_observed, provider, request, response=True)

    @contextmanager
    def changed_cache(self, change):
        original = self.cache.read_bytes()
        info = self.cache.stat()
        added = self.cache.with_name('cmw_acceptance_added.' + sys.implementation.cache_tag + '.pyc')
        self.assertFalse(added.exists())
        try:
            if change == 'content':
                self.cache.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
                os.utime(self.cache, ns=(info.st_atime_ns, info.st_mtime_ns))
            elif change == 'addition':
                added.write_bytes(original)
            elif change == 'deletion':
                self.cache.unlink()
            else:
                raise AssertionError('Unknown injected cache change')
            yield
        finally:
            if change != 'addition':
                self.cache.write_bytes(original)
                self.cache.chmod(info.st_mode & 0o7777)
                os.utime(self.cache, ns=(info.st_atime_ns, info.st_mtime_ns))
            if added.exists():
                added.unlink()
            self.assertEqual(self.cache.read_bytes(), original)

    def refused_change(self, change):
        categories, completed = {}, set()
        for strategy in ('cheap', 'full'):
            with self.subTest(strategy=strategy):
                verifier, request, _ = self.begin()
                with self.changed_cache(change), self.assertRaises(self.runtime.RuntimeRefusal) as refused:
                    self.finish(verifier, request, strategy)
                categories[strategy] = refused.exception.evidence['category']
                self.assertTrue(verifier.tainted)
                # Restoring the cache cannot clear this observer's refusal.
                with self.assertRaisesRegex(self.runtime.RuntimeRefusal, 'fresh_isolated_observer_required'):
                    verifier.verify(self.identity.runtime_observed)
                completed.add(strategy)
        self.assertEqual(completed, {'cheap', 'full'})
        self.assertEqual(set(categories), {'cheap', 'full'})
        self.assertEqual(categories['cheap'], categories['full'])
        self.evidence.append({'case': change, 'status': 'PASS', 'classification': categories,
                              'sticky_after_restoration': True})

    def test_unchanged_precompiled_installation_accepts_request(self):
        completed = []
        for strategy in ('cheap', 'full'):
            with self.subTest(strategy=strategy):
                verifier, request, record = self.begin()
                self.assertEqual(self.finish(verifier, request, strategy), record)
                self.assertFalse(verifier.tainted)
                self.assertEqual(request, {})
                completed.append(strategy)
        if len(completed) == 2:
            self.evidence.append({'case': 'unchanged', 'status': 'PASS', 'strategies': completed})

    def test_changed_precompiled_content_is_sticky_and_matches_full_scan(self):
        self.refused_change('content')

    def test_added_precompiled_member_is_sticky_and_matches_full_scan(self):
        self.refused_change('addition')

    def test_deleted_precompiled_member_is_sticky_and_matches_full_scan(self):
        self.refused_change('deletion')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owned-prefix', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = {'python': sys.version, 'executable': sys.executable, 'isolated': bool(sys.flags.isolated),
              'dont_write_bytecode': sys.dont_write_bytecode, 'tests_run': 0, 'status': 'FAILED'}
    try:
        root, cache, identity, runtime, count = prepare_installation(args.owned_prefix)
        report.update(package=str(root), precompiled_sources=count, loaded_cache=str(cache))
        case = InstalledBytecodeBoundaryTests
        case.root, case.cache, case.identity, case.runtime = root, cache, identity, runtime
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(case)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        report.update(tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                      cases=case.evidence, status='PASS' if result.wasSuccessful() else 'FAILED')
        return 0 if result.wasSuccessful() else 1
    finally:
        args.output.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2))


if __name__ == '__main__':
    raise SystemExit(main())
