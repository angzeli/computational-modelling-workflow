"""Test-owned macmon-compatible executable; never accesses hardware."""
from pathlib import Path
import sys


def write_fake_macmon(path, *, mode='valid', temperature=63.2, pause=0.0):
    path = Path(path)
    source = f'''#!{sys.executable}
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
with Path(__file__ + '.calls').open('a') as log:
    log.write(json.dumps(args) + '\\n')
mode = {mode!r}
if args == ['--version']:
    print('macmon 9.0.0' if mode == 'version' else 'macmon 0.10.0')
elif args == ['pipe', '--help']:
    print('Output metrics in JSON format: --samples --interval' if mode != 'help' else 'Unsupported')
elif args == ['pipe', '--samples', '1', '--interval', '1000']:
    time.sleep({pause!r})
    if mode == 'hang': time.sleep(20)
    if mode == 'fail':
        sys.stderr.write('PRIVATE provider diagnostic')
        sys.exit(3)
    if mode == 'overflow':
        sys.stdout.write('x'*70000)
    elif mode == 'stderr':
        sys.stderr.write('x'*5000)
    elif mode == 'malformed': print('{{"temp":')
    elif mode == 'multiple': print('{{"temp":{{}}}}\\n{{"temp":')
    elif mode == 'empty': pass
    elif mode == 'missing': print('{{"temp":{{"gpu_temp_avg":50}}}}')
    else:
        value = None if mode == 'null' else {temperature!r}
        if mode == 'changes':
            counter = Path(__file__ + '.samples')
            value = None if counter.exists() else value
            counter.write_text('1')
        print(json.dumps({{'temp':{{'cpu_temp_avg':value}}}}))
else:
    sys.exit(8)
'''
    path.write_text(source)
    path.chmod(0o700)
    return path
