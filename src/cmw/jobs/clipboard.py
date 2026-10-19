"""Bounded, explicit clipboard delivery with honest backend-specific results."""
from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
import sys

from .snapshot import safe_snapshot_text


@dataclass(frozen=True)
class CopyResult:
    status: str
    message: str


def verified_local_macos(*, platform=None, environ=None, console_uid=None, uid=None, tty=None):
    """Only a direct, known desktop terminal owned by the current console user."""
    env = os.environ if environ is None else environ
    if (sys.platform if platform is None else platform) != 'darwin':
        return False
    if any(env.get(key) for key in ('SSH_CONNECTION', 'SSH_CLIENT', 'SSH_TTY', 'TMUX', 'STY')):
        return False
    if env.get('TERM_PROGRAM') not in {'Apple_Terminal', 'iTerm.app'}:
        return False
    if not (sys.stdout.isatty() if tty is None else tty):
        return False
    try:
        owner = os.stat('/dev/console').st_uid if console_uid is None else console_uid
        return owner == (os.getuid() if uid is None else uid) and owner != 0
    except OSError:
        return False


def copy_text(text, *, local_verified=None, runner=None, osc52=None):
    """No shell, clipboard reads, file export, or success from an app property."""
    text = safe_snapshot_text(text)
    if len(text.encode('utf-8')) > 1024 * 1024:
        return CopyResult('UNAVAILABLE', 'Copy exceeds the 1 MiB clipboard bound; select a smaller passage.')
    if local_verified is None:
        local_verified = verified_local_macos()
    if local_verified:
        try:
            result = (subprocess.run if runner is None else runner)(
                ['/usr/bin/pbcopy'], input=text, text=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=2, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CopyResult('FAILED', f'Local clipboard failed ({type(exc).__name__}); selectable preview retained.')
        if result.returncode == 0:
            return CopyResult('LOCAL_ACCEPTED', 'Copy accepted by local clipboard backend.')
        return CopyResult('FAILED', 'Local clipboard failed; selectable preview retained.')
    if osc52 is not None:
        try:
            osc52(text)
        except Exception as exc:
            return CopyResult('FAILED', f'OSC52 request failed ({type(exc).__name__}); selectable preview retained.')
        return CopyResult('OSC52_REQUESTED', 'OSC52 copy requested; terminal delivery is unverified.')
    return CopyResult('UNAVAILABLE', 'Clipboard unavailable; select the frozen text or use jobs progress --snapshot.')
