"""Bounded publication screen for tracked source and built wheel/sdist members.

This checks obvious home-path leakage and accidental runtime/engine artifacts;
it is not a general secrets or licensing scanner. Reduced parser session logs
under tests/fixtures/multiwfn/excited_states are intentional publication inputs.
"""
import argparse
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import zipfile


PRIVATE_HOME = re.compile(rb"/(?:Users|home)/[^/\s\"'<>]+")
PRIVATE_TEMP = re.compile(rb"/(?:private/)?var/folders/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/")
EXCLUDED_PARTS = {'.git', '.venv', '__pycache__', '.pytest_cache', '.mypy_cache',
                  '.ruff_cache', 'runs', '.DS_Store'}
REDUCED_LOGS = {
    f'tests/fixtures/multiwfn/excited_states/multiwfn_{version}_{case}.session.log'
    for version, cases in (
        ('3_8', ('s1_nto', 't2_nto', 's1_hea', 't1_hea', 't2_hea')),
        ('2026_7_15', ('s1_nto', 't2_nto', 's1_hea', 't2_hea')))
    for case in cases
}
MAX_MEMBER_BYTES = 8 * 1024 * 1024


def findings(name, data):
    path = PurePosixPath(name)
    problems = []
    if PRIVATE_HOME.search(data):
        problems.append('local-home path in content')
    if PRIVATE_TEMP.search(data):
        problems.append('machine-specific temporary path in content')
    if b'CMW-' + b'forensic-audit-' in data:
        problems.append('private audit path in content')
    if set(path.parts) & EXCLUDED_PARTS or path.is_absolute() or '..' in path.parts:
        problems.append('runtime/cache or unsafe member path')
    if path.name.lower() in {'vasp_std', 'vasp_gam', 'vasp_ncl', 'orca', 'multiwfn'} or \
            path.name.upper().startswith(('POTCAR', 'WAVECAR')) or path.suffix.lower() in {
            '.gbw', '.db', '.sqlite', '.sqlite3', '.pyc', '.pyo'}:
        problems.append('engine/state artifact')
    if path.suffix == '.log' and name not in REDUCED_LOGS:
        problems.append('runtime log outside reduced parser fixtures')
    return [f'{name}: {problem}' for problem in problems]


def source_findings(root):
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
    problems = []
    for name in filter(None, names):
        path = root/name
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            problems.append(f'{name}: non-regular tracked input')
        elif metadata.st_size > MAX_MEMBER_BYTES:
            problems.append(f'{name}: exceeds bounded inspection size')
        else:
            problems.extend(findings(name, path.read_bytes()))
    return problems


def artifact_findings(artifact):
    problems = []
    if artifact.suffix == '.whl':
        with zipfile.ZipFile(artifact) as archive:
            for member in archive.infolist():
                if not member.is_dir():
                    if member.file_size > MAX_MEMBER_BYTES:
                        problems.append(f'{member.filename}: exceeds bounded inspection size')
                    else:
                        problems.extend(findings(member.filename, archive.read(member)))
    else:
        with tarfile.open(artifact, 'r:gz') as archive:
            for member in archive:
                if PurePosixPath(member.name).is_absolute() or '..' in PurePosixPath(member.name).parts:
                    problems.append(f'{member.name}: unsafe archive member path')
                    continue
                # An sdist wraps repository-relative names in one project directory.
                name = member.name.split('/', 1)[-1]
                if member.isfile():
                    if member.size > MAX_MEMBER_BYTES:
                        problems.append(f'{name}: exceeds bounded inspection size')
                        continue
                    with archive.extractfile(member) as handle:
                        problems.extend(findings(name, handle.read()))
                elif not member.isdir():
                    problems.append(f'{name}: non-regular archive member')
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--artifact', type=Path, action='append', default=[])
    args = parser.parse_args()
    if args.source_root is None and not args.artifact:
        parser.error('provide --source-root and/or --artifact')
    problems = source_findings(args.source_root) if args.source_root else []
    for artifact in args.artifact:
        problems.extend(artifact_findings(artifact))
    if problems:
        print('\n'.join(problems))
        return 1
    print('Publication hygiene: passed (bounded source/artifact screen)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
