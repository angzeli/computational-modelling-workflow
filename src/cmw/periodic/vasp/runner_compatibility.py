"""Read-only interface evidence for the explicit run-vasp.sh managed route.

Runner code is read as data, never imported, sourced or executed, even for help.
Literal argparse declarations describe an interface, not its runtime behavior.
"""
from __future__ import annotations

import ast
from importlib import metadata
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from .result_runner import DIALECT, validate_runner_argv
from .result_sources import SnapshotReader


_SOURCE_LIMIT = 512 * 1024
_PORT_FILES = ('run-vasp.sh', 'run_vasp.py', 'environment.sh', 'launch.py', 'observables.py')
_WRAPPER_PREFIX = ['set -euo pipefail', '. "$(dirname -- "$0")/environment.sh"']
_WRAPPER_EXEC = {
    'exec python3 -B "$PORT_ROOT/scripts/run_vasp.py" "$@"',
    'exec "${CMW_MANAGED_PYTHON:-python3}" -B "$PORT_ROOT/scripts/run_vasp.py" "$@"',
}
_PORT_ROOT = 'PORT_ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)'


def _environment_relationship(text):
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    if not lines or lines[0] != _PORT_ROOT:
        raise ValueError('Environment does not establish the selected checkout as PORT_ROOT')
    # Accept only inert assignments/exports after the one known root expression.
    assignment = r'[A-Z_][A-Z0-9_]*=(?:"[A-Za-z0-9_./:$ +\-]*"|[A-Za-z0-9_./:$+\-]+)'
    for line in lines[1:]:
        if 'PORT_ROOT' in line or not (re.fullmatch(r'(?:export )?' + assignment + r'(?: ' + assignment + r')*', line)
                                             or re.fullmatch(r'unset [A-Z_][A-Z0-9_]*(?: [A-Z_][A-Z0-9_]*)*', line)):
            raise ValueError('Environment contains unassessed shell operations; wrapper association is unknown')


def _source(path, role, records):
    reader = SnapshotReader(path, role, max_bytes=_SOURCE_LIMIT)
    records.append(reader.record)
    with reader:
        text = ''.join(line.text for line in reader)
    if not reader.record['stable'] or reader.record['coverage'] != 'complete':
        raise ValueError(f'Incomplete or changing source: {path}')
    return text


def _git(directory, *arguments):
    # These are fixed metadata queries, never runner probes. Disable optional
    # index writes, user config and executable fsmonitor/diff integrations.
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS='0')
    result = subprocess.run(
        ['git', '--no-optional-locks', '--literal-pathspecs', '-c', 'core.fsmonitor=false',
         '-c', 'core.hooksPath=' + os.devnull, '-C', str(directory), *arguments],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, timeout=3, check=False,
    )
    # Queries return one path, one revision, known filenames or no output.
    if len(result.stdout) > 65536:
        raise ValueError('Git identity output exceeds 64 KiB')
    return result.returncode, result.stdout.decode('utf-8', errors='replace').strip()


def _repository(path, files=()):
    result = {'locator': str(path), 'established': False, 'commit': None,
              'tracked_worktree_dirty': None, 'relevant_source_dirty': None,
              'untracked_files': 'not enumerated', 'untracked_relevant_sources': []}
    try:
        code, root = _git(path if path.is_dir() else path.parent, 'rev-parse', '--show-toplevel')
        if code:
            return result
        code, commit = _git(Path(root), 'rev-parse', '--verify', 'HEAD')
        if code:
            return result
        result.update(established=True, repository=root, commit=commit)
        # Worktree diff/status can invoke configured clean/process filters. Only
        # compare known source bytes, without filters; do not scan all tracked files.
        result['tracked_worktree_assessment'] = 'not assessed; only relevant source bytes are compared'
        if files:
            relative = [str(file.relative_to(root)) for file in files]
            dirty = False
            for file in relative:
                code, expected = _git(Path(root), 'rev-parse', '--verify', 'HEAD:' + file)
                if code:
                    result['untracked_relevant_sources'].append(file)
                    continue
                observed_stat = (Path(root) / file).lstat()
                if not stat.S_ISREG(observed_stat.st_mode) or observed_stat.st_size > _SOURCE_LIMIT:
                    raise ValueError('Relevant source is not a bounded regular file')
                code, observed = _git(Path(root), 'hash-object', '--no-filters', '--', file)
                if code:
                    dirty = None
                elif expected != observed:
                    dirty = True
            result['relevant_source_dirty'] = True if result['untracked_relevant_sources'] else dirty
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result['error'] = str(exc)
    return result


def _parser_options(text):
    """Recognize the port's straight-line parser factory without evaluating it."""
    tree = ast.parse(text)
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                constants[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                pass
    factories = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'parser']
    if len(factories) != 1:
        raise ValueError('Expected one explicit parser() factory; interface is unknown')
    factory = factories[0]
    if factory.decorator_list or factory.args.args or factory.args.vararg or factory.args.kwarg:
        raise ValueError('Dynamic parser factory is not assessed')
    options, parser_name = {}, None
    for index, statement in enumerate(factory.body):
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
            continue
        if (isinstance(statement, ast.Assign) and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name) and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Attribute)
                and isinstance(statement.value.func.value, ast.Name)
                and statement.value.func.value.id == 'argparse' and statement.value.func.attr == 'ArgumentParser'
                and parser_name is None):
            parser_name = statement.targets[0].id
            continue
        if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Name):
            if statement.value.id == parser_name and index == len(factory.body) - 1:
                return options
            raise ValueError('Parser return is not the recognized argument parser')
        call = statement.value if isinstance(statement, ast.Expr) else None
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name) and call.func.value.id == parser_name
                and call.func.attr == 'add_argument' and call.args):
            raise ValueError('Dynamic parser construction is not assessed')
        names = [ast.literal_eval(argument) for argument in call.args]
        if not all(isinstance(name, str) and name.startswith('--') for name in names):
            raise ValueError('Unsupported argument declaration')
        declaration = {}
        for keyword in call.keywords:
            if keyword.arg == 'type':
                declaration['type'] = (keyword.value.id if isinstance(keyword.value, ast.Name)
                                       else {'unresolved': ast.unparse(keyword.value)})
            elif keyword.arg in {'action', 'help', 'required', 'default', 'choices', 'nargs'}:
                try:
                    declaration[keyword.arg] = ast.literal_eval(keyword.value)
                except (ValueError, TypeError):
                    value = keyword.value
                    # Resolve only the known tuple of literal restart dictionary
                    # keys; never evaluate the runner's expression or imports.
                    if (keyword.arg == 'choices' and isinstance(value, ast.Call)
                            and isinstance(value.func, ast.Name) and value.func.id == 'tuple'
                            and len(value.args) == 1 and not value.keywords
                            and isinstance(value.args[0], ast.Name)
                            and isinstance(constants.get(value.args[0].id), (dict, list, tuple))):
                        declaration[keyword.arg] = list(constants[value.args[0].id])
                    else:
                        declaration[keyword.arg] = {'unresolved': ast.unparse(value)}
            elif keyword.arg is None:
                raise ValueError('Expanded parser keyword arguments are not assessed')
        for name in names:
            if name in options:
                raise ValueError('Duplicate option declaration is ambiguous')
            options[name] = declaration
    raise ValueError('Parser factory has no recognized return')


def check_runner(runner, argv, *, project_root=None):
    """Inspect a selected port checkout and intended argv; grant no run authority."""
    runner = Path(runner).expanduser().absolute()
    records = []
    result = {
        'status': 'unknown', 'route': 'run-vasp.sh-managed-foreground', 'exit_code': 2,
        'interface_compatibility': {'status': 'UNKNOWN', 'entry_point_located': False,
                                    'basis': 'static argparse declarations; runner not executed'},
        'managed_lifecycle_qualification': {
            'status': 'NOT_ESTABLISHED',
            'evidence_references': ['tests/jobs/test_session_runtime.py', 'tests/jobs/test_session_ownership.py'],
            'reason': 'CMW synthetic ownership regressions do not qualify this external source combination or native MPI.'},
        'result_binding_qualification': {
            'status': 'NOT_ESTABLISHED', 'dialect': DIALECT,
            'evidence_references': ['tests/periodic/vasp/test_result_runner.py', 'tests/periodic/vasp/test_result_runner_finalization.py'],
            'reason': 'Requires original native Jobs completion, exact runner/input/output association and source reverification.'},
        'source_combination': {}, 'observed_capabilities': {}, 'missing_requirements': [],
        'unassessed_properties': [
            'Parser declarations are not proof of runtime behavior or historical source equivalence.',
            'Binary/MPI discovery, actual ranks/threads and effective NCORE/KPAR are not observed.',
            'Input validity, staging, cancellation/drainage and native MPI/session topology are not tested.',
            'Scientific policy, result metadata, native receipt and finalization/verification are not evaluated.',
            'Package version and filesystem locators are not source-equivalence pins.'],
        'side_effects_performed': [],
    }
    import cmw
    cmw_origin = Path(cmw.__file__).resolve()
    cmw_identity = _repository(cmw_origin, [Path(__file__).with_name(name) for name in ('runner_compatibility.py', 'result_runner.py')])
    cmw_identity['module_origin'] = str(cmw_origin)
    cmw_identity['sources'] = []
    for name in ('runner_compatibility.py', 'result_runner.py'):
        try:
            _source(Path(__file__).with_name(name), 'cmw_source:' + name, cmw_identity['sources'])
        except (OSError, ValueError) as exc:
            cmw_identity['source_error'] = str(exc)
    try:
        distribution = metadata.distribution('computational-modelling-workflow')
        cmw_identity.update(package_version=distribution.version,
                            distribution_location=str(distribution.locate_file('')),
                            distribution_direct_url=json.loads(distribution.read_text('direct_url.json') or 'null'))
    except (metadata.PackageNotFoundError, OSError, ValueError):
        cmw_identity['package_version'] = None
    result['source_combination']['cmw'] = cmw_identity
    source_files = [runner.parent / name for name in _PORT_FILES]
    result['source_combination']['runner'] = _repository(runner, source_files)
    result['source_combination']['runner']['sources'] = records
    result['source_combination']['project'] = (_repository(Path(project_root).expanduser().resolve())
                                               if project_root is not None else {'established': False, 'reason': 'not supplied'})
    interface = result['interface_compatibility']
    try:
        source, output = validate_runner_argv([str(runner), *argv])
        if not source.is_dir():
            raise ValueError('The explicit input directory must exist; use check-inputs separately for its contents')
        if os.path.lexists(output):
            raise ValueError('The intended output must be a new directory, not an existing path')
    except (ValueError, TypeError, OverflowError) as exc:
        interface.update(status='INCOMPATIBLE', reason=str(exc))
        result['missing_requirements'].append(str(exc))
        result['status'] = 'incompatible'
        return result
    result['observed_capabilities']['intended_handoff'] = {'argv': [str(runner), *argv], 'input': str(source), 'output': str(output)}
    try:
        contents = {}
        for path in source_files:
            contents[path.name] = _source(path, 'runner_source:' + path.name, records)
            if path == runner:
                interface['entry_point_located'] = True
        lines = [line.strip() for line in contents['run-vasp.sh'].splitlines() if line.strip() and not line.lstrip().startswith('#')]
        if len(lines) != 3 or lines[:2] != _WRAPPER_PREFIX or lines[2] not in _WRAPPER_EXEC:
            raise ValueError('Shell wrapper is outside the recognized run-vasp.sh delegation; no shell/help probe performed')
        _environment_relationship(contents['environment.sh'])
        interface['wrapper_relationship'] = 'run-vasp.sh -> environment.sh -> scripts/run_vasp.py'
        options = _parser_options(contents['run_vasp.py'])
        result['observed_capabilities']['argument_declarations'] = options
        # argv has already passed the existing result-association grammar.
        intended = dict(zip([item for item in argv if item != '--managed-foreground'][::2],
                            [item for item in argv if item != '--managed-foreground'][1::2]))
        required = set(intended) | {'--managed-foreground'}
        for name, declaration in options.items():
            if not isinstance(declaration.get('required', False), bool):
                raise ValueError(f'{name} has dynamic required constraints; compatibility is unknown')
            if declaration.get('required') and not any(options.get(supplied) is declaration for supplied in required):
                result['missing_requirements'].append(f'Required declared option {name} is absent from the intended argv')
        missing = sorted(required - options.keys())
        for name in missing:
            result['missing_requirements'].append(f'Required option {name} is absent from parser() declarations')
        for name, value in intended.items():
            declaration = options.get(name, {})
            if declaration.get('type', 'str') not in ('str', 'int', 'float', 'Path'):
                raise ValueError(f'{name} has an unassessed argument type; compatibility is unknown')
            choices = declaration.get('choices')
            if isinstance(choices, dict) or isinstance(declaration.get('action'), dict) or isinstance(declaration.get('nargs'), dict):
                raise ValueError(f'{name} has dynamic argument constraints; compatibility is unknown')
            if isinstance(choices, (list, tuple)) and value not in choices:
                result['missing_requirements'].append(f'{name} does not declare the required value {value!r}')
            if declaration.get('action', 'store') != 'store' or declaration.get('nargs') not in (None, 1):
                result['missing_requirements'].append(f'{name} is not a recognized single-value argument')
        managed = options.get('--managed-foreground', {})
        if managed and managed.get('action') != 'store_true':
            result['missing_requirements'].append('--managed-foreground must be declared as a boolean flag')
        result['observed_capabilities']['managed_contract_declaration'] = {
            'status': 'IDENTIFIED' if isinstance(managed.get('help'), str) else 'NOT_IDENTIFIED',
            'text': managed.get('help'), 'qualification': 'declaration only; implementation not qualified'}
        result['observed_capabilities']['thread_propagation'] = 'No --threads option in the supported binding route; inspect the shell environment separately.'
        if result['missing_requirements']:
            result['status'] = 'incompatible'
            interface['status'] = 'INCOMPATIBLE'
        else:
            result.update(status='interface-compatible-unqualified', exit_code=0)
            interface['status'] = 'COMPATIBLE'
        if result['source_combination']['runner'].get('relevant_source_dirty'):
            interface['source_warning'] = 'Observed working-tree support is not a committed compatibility result.'
    except (OSError, ValueError, SyntaxError, TypeError, RecursionError) as exc:
        interface['reason'] = str(exc)
    return result


def format_check(result):
    interface = result['interface_compatibility']
    lines = [f"Runner argument interface: {interface['status']} (static declarations only)"]
    lines.extend(result['missing_requirements'])
    for key in ('reason', 'source_warning'):
        if interface.get(key):
            lines.append(interface[key])
    for name, source in result['source_combination'].items():
        lines.append(f"{name} source: commit={source.get('commit') or 'NOT ESTABLISHED'}; "
                     f"relevant dirty={source.get('relevant_source_dirty')}; "
                     f"locator={source.get('module_origin') or source.get('locator') or 'not supplied'}")
    lines.extend(['Managed lifecycle qualification: NOT ESTABLISHED',
                  'Result binding qualification: NOT ESTABLISHED',
                  'No runner probe, staging, execution or Jobs mutation performed.',
                  'Use an exact tested source combination; this result does not authorize or qualify execution.'])
    return '\n'.join(lines)
