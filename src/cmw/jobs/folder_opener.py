"""Revalidated directory navigation, independent of engine and Jobs execution."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import os
import subprocess
import sys

from .locations import Location, discover, inspect_path
from .store import JobsError


@dataclass(frozen=True)
class OpenResult:
    status: str
    reason: str
    path: str | None = None

    def to_dict(self):
        return asdict(self)


def request_open(path, *, platform=None, runner=None):
    """Request macOS folder opening; executable/installable bundles are excluded.

    Success confirms only the open request, not a visually verified Finder window.
    """
    path = str(path)
    if not Path(path).is_absolute() or '\0' in path or '://' in path:
        return OpenResult('LOCATION_UNKNOWN', 'Only an absolute filesystem directory may be opened', path)
    canonical, status, identity, _, detail = inspect_path(Path(path), 'directory')
    if status != 'AVAILABLE':
        return OpenResult(status, detail, canonical)
    if canonical != os.path.abspath(path):
        return OpenResult('LOCATION_CHANGED', 'Directory target redirected before opening; select it again', canonical)
    if Path(canonical).suffix.lower() in {'.app', '.workflow', '.scptd', '.saver', '.prefpane', '.pkg', '.mpkg'}:
        return OpenResult('LOCATION_UNKNOWN', 'Executable, installer or settings bundles are not folder navigation targets', canonical)
    if (sys.platform if platform is None else platform) != 'darwin':
        return OpenResult('UNSUPPORTED_DESKTOP', 'No supported graphical opener; use the full directory path', canonical)
    try:
        latest = Path(canonical).stat()
        if (latest.st_dev, latest.st_ino) != identity or str(Path(canonical).resolve()) != canonical:
            return OpenResult('LOCATION_CHANGED', 'Directory changed before the open request', canonical)
        result = (subprocess.run if runner is None else runner)(
            ['/usr/bin/open', '--', canonical], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return OpenResult('OPEN_FAILED', f'Folder open request failed: {exc}', canonical)
    if result.returncode:
        message = (result.stderr or result.stdout or f'exit {result.returncode}').strip()[:1024]
        return OpenResult('OPEN_FAILED', f'Folder open request failed: {message}', canonical)
    return OpenResult('OPEN_REQUESTED', 'Folder open requested', canonical)


def open_location(store, selected_location, *, descriptors=(), resolvers=None, opener=None):
    """Re-read an immutable selection, then open only its verified directory."""
    selected = selected_location
    if not isinstance(selected, Location):
        raise TypeError('Opening requires a Location from discover()')
    if str(store.root) != selected.state_directory:
        return OpenResult('LOCATION_CHANGED', 'Selected Jobs state differs; select the location again', selected.path)
    try:
        result = discover(store, selected.job_id, expected_attempt_id=selected.attempt_id,
                          descriptors=descriptors, resolvers=resolvers)
    except (JobsError, OSError) as exc:
        return OpenResult('LOCATION_CHANGED', f'Selected location is stale: {exc}', selected.path)
    current = next((item for item in result.locations if item.key == selected.key), None)
    if current is None:
        return OpenResult('LOCATION_CHANGED', 'Location declaration or target changed; select it again', selected.path)
    if current.path is None or current.accessibility != 'AVAILABLE':
        return OpenResult(current.accessibility, current.detail or 'Location unavailable', current.path)
    if ((selected.target_identity is not None and selected.target_identity != current.target_identity) or
            (selected.directory_identity is not None and selected.directory_identity != current.directory_identity)):
        return OpenResult('LOCATION_CHANGED', 'Selected filesystem object was replaced; select it again', current.path)
    canonical, status, identity, directory_identity, detail = inspect_path(
        Path(current.declared_path), current.kind)
    if (canonical != current.path or identity != current.target_identity or
            directory_identity != current.directory_identity):
        return OpenResult('LOCATION_CHANGED', 'Selected target changed before opening; select it again', canonical)
    if status != 'AVAILABLE':
        return OpenResult(status, detail, canonical)
    directory = str(Path(canonical).parent) if current.kind == 'file' else canonical
    try:
        opened = (request_open if opener is None else opener)(directory)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return OpenResult('OPEN_FAILED', f'Folder open request failed: {exc}', directory)
    if not isinstance(opened, OpenResult):
        return OpenResult('OPEN_FAILED', 'Folder opener did not return a verifiable request result', directory)
    return opened
