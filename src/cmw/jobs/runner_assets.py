"""The same reviewed shell sources in a checkout, sdist or installed wheel."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat

# Runtime identities are streamed, independently of portable input transfer limits.
MAX_IDENTITY_BYTES = 8 * 1024**3
_CLOSURES = {
    'orca/run_orca.sh': ('orca/run_orca.sh',),
    'workflows/run_opt_freq_sp.sh': ('workflows/run_opt_freq_sp.sh', 'orca/run_orca.sh'),
    'multiwfn/run_ifct.sh': ('multiwfn/run_ifct.sh', 'multiwfn/multiwfn_runtime.sh'),
    **{f'workflows/{name}': (f'workflows/{name}', 'workflows/multiwfn_cube_workflow.sh',
                            'multiwfn/multiwfn_runtime.sh') for name in
       ('generate_homo_lumo_cubes.sh', 'generate_esp_cubes.sh', 'generate_igmh_cubes.sh')},
}


def file_identity(path, *, max_bytes=MAX_IDENTITY_BYTES):
    """Hash one regular file with a byte budget and read-race checks; never execute it."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Dependency identity requires an absolute path')
    canonical = path.resolve(strict=True)
    fd = os.open(canonical, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
    digest, count = hashlib.sha256(), 0
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise ValueError(f'Dependency must be a regular file of at most {max_bytes} bytes: {path}')
        while chunk := stream.read(min(1024**2, max_bytes - count + 1)):
            count += len(chunk)
            if count > max_bytes:
                raise ValueError(f'Dependency grew beyond the identity byte limit: {path}')
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if signature(before) != signature(after) or signature(after) != signature(canonical.stat()):
        raise ValueError(f'Dependency changed during identity reading: {path}')
    if path.resolve(strict=True) != canonical:
        raise ValueError(f'Dependency path changed during identity reading: {path}')
    return {'path': str(path), 'sha256': digest.hexdigest(), 'size_bytes': count}


def runner_path(relative):
    """Select assets beside this imported package, never a separately configured checkout."""
    if relative not in _CLOSURES:
        raise ValueError(f'Unsupported molecular runner: {relative}')
    package = Path(__file__).resolve().parents[1]
    packaged = package / '_runner_assets' / 'scripts'
    if packaged.is_dir():
        path = packaged / relative
    else:
        # Only the checkout containing this module can supply uninstalled assets.
        if package.parent.name != 'src':
            raise ValueError('Installed molecular runner assets are unavailable')
        path = package.parent.parent / 'scripts' / relative
    if not path.is_file():
        raise ValueError(f'Required molecular runner is unavailable: {path}')
    return path


def runner_dependencies(relative):
    root = runner_path(relative)
    for _ in Path(relative).parts:
        root = root.parent
    return [file_identity(root / name, max_bytes=1024**2)
            for name in (*_CLOSURES[relative], 'runtime_python.sh')]
