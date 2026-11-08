"""Local OpenSSH integration without an SSH connection or network access."""
import os
import shutil
import sys


def local_sftp_command(case):
    """Find executable client/server tools; CI requires these integration cases."""
    server_candidates = {
        'linux': ('/usr/lib/openssh/sftp-server', '/usr/libexec/sftp-server'),
        'darwin': ('/usr/libexec/sftp-server', '/usr/lib/openssh/sftp-server'),
    }.get(sys.platform, ('/usr/libexec/sftp-server', '/usr/lib/openssh/sftp-server'))
    candidates = {'sftp': ('/usr/bin/sftp', 'sftp'),
                  'sftp-server': (*server_candidates, 'sftp-server')}
    resolved = {}
    missing = []
    for name, paths in candidates.items():
        for path in paths:
            executable = shutil.which(path)
            if executable:
                resolved[name] = executable
                break
        else:
            missing.append(f'{name} (searched: {", ".join(paths)})')
    if missing:
        reason = 'Missing executable for local OpenSSH integration: ' + '; '.join(missing)
        if os.environ.get('CMW_REQUIRE_SFTP_INTEGRATION') == '1':
            case.fail(reason)
        case.skipTest(reason)
    # -D starts the server directly over local pipes, never SSH or a socket.
    return [resolved['sftp'], '-b', '-', '-D', resolved['sftp-server']]
