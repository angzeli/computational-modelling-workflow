"""Owner-local VASP study evidence and one explicit, precision-qualified rule.

This module observes existing files only. It does not finalize results, select a
scientific tolerance, authenticate caller-owned records, or control execution.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
import csv
import io
import json
import math
import os
from pathlib import Path

from cmw.core.provenance import stable_hash
from cmw.periodic.vasp.inputs import INPUT_NAMES, check_input_bytes
from cmw.periodic.vasp.preparation import compare_inputs
from cmw.periodic.vasp.result_execution import inspect_execution
from cmw.periodic.vasp.result_finalization import inspect_with_policy, verify_finalization
from cmw.periodic.vasp.result_policy import load_policy
from cmw.periodic.vasp.result_sources import revalidate_sources
from .review_engines import _Sources


ENERGIES = frozenset(('free_energy', 'without_entropy', 'sigma_to_zero'))
MAX_CASES = 64
MAX_INPUT_BYTES = 64 * 1024 * 1024
_RUNTIME = frozenset(('NCORE', 'KPAR', 'NPAR', 'SYSTEM'))
_CHARGE = frozenset(('NELECT',))
_SPIN = frozenset(('ISPIN', 'MAGMOM', 'NUPDOWN', 'LSORBIT', 'LNONCOLLINEAR'))
_HAMILTONIAN = frozenset(('GGA', 'METAGGA', 'LHFCALC', 'AEXX', 'HFSCREEN', 'LDAU',
    'LDAUTYPE', 'LDAUL', 'LDAUU', 'LDAUJ', 'IVDW', 'LUSE_VDW', 'LDIPOL', 'IDIPOL', 'DIPOL'))


def validate_comparison_spec(spec):
    """Validate caller-selected units, normalization, policy and bounded rule."""
    metric, normalization, rule = (spec.get(key) for key in ('metric', 'normalization', 'comparison_rule'))
    if (not isinstance(metric, dict) or set(metric) != {'name', 'unit'}
            or not isinstance(metric['name'], str) or metric['name'] not in ENERGIES or metric['unit'] != 'eV'):
        raise ValueError('Select exactly one supported VASP evaluated energy in eV')
    if (not isinstance(normalization, dict) or set(normalization) != {'kind'}
            or not isinstance(normalization['kind'], str) or normalization['kind'] not in {'none', 'per_atom'}):
        raise ValueError('Supported normalization is none or per_atom within a fixed structure/composition')
    if (not isinstance(rule, dict) or set(rule) != {'kind', 'reference_case', 'tolerance'}
            or rule['kind'] != 'absolute-to-reference' or not isinstance(rule['reference_case'], str)
            or not rule['reference_case'] or type(rule['tolerance']) not in (int, float)
            or not math.isfinite(rule['tolerance']) or rule['tolerance'] <= 0):
        raise ValueError('An explicit reference case and positive finite absolute-to-reference tolerance are required')
    declaration = spec.get('scientific_policy')
    if (not isinstance(declaration, dict) or set(declaration) != {'path', 'sha256'}):
        raise ValueError('Select an exact scientific policy path and sha256')
    policy, source = load_policy(declaration['path'])
    if source['sha256'] != declaration['sha256']:
        raise ValueError('Scientific policy differs from its reviewed bytes')
    if policy['calculation'] != 'static' or 'energy:' + metric['name'] not in policy['artifact_roles']:
        raise ValueError('Study policy must cover static calculations and the exact selected energy role')
    return {'policy': policy, 'policy_id': stable_hash(policy), 'source': source}


def _status(value):
    return 'PASS' if value is True else 'FAIL' if value is False else 'UNKNOWN'


def _input_semantics(files, checked, effective):
    inputs = checked['inputs']
    poscar, settings = inputs['POSCAR'], inputs['INCAR']['settings']
    combined = {**settings, **effective}
    return {
        'geometry': poscar.get('cartesian_coordinates'), 'cell': poscar.get('cell'),
        'composition': dict(Counter(poscar.get('atom_species', ()))),
        'species_order': poscar.get('atom_species'), 'constraints': poscar.get('selective_dynamics'),
        'charge': {k: combined[k] for k in sorted(_CHARGE & combined.keys())},
        'spin': {k: combined[k] for k in sorted(_SPIN & combined.keys())},
        'Hamiltonian': {k: combined[k] for k in sorted(_HAMILTONIAN & combined.keys())},
        'potential_bytes': checked['identities'].get('POTCAR'),
        'k_sampling': {k: inputs['KPOINTS'].get(k) for k in ('mode', 'mesh', 'shift')},
        'numerical_controls': {k: v for k, v in combined.items()
            if k not in _RUNTIME | _CHARGE | _SPIN | _HAMILTONIAN},
        'unknown_tags': inputs['INCAR'].get('unknown_tags', []),
    }


def _verify_runtime_incar(bound, files, expected_inputs, expected_runtime, sources):
    preparation = bound.get('preparation') or {}
    declared = bound.get('resolved_spec') or {}
    overlay = declared.get('expected_runtime_overlay')
    if (not isinstance(expected_runtime, dict) or not isinstance(overlay, dict)
            or overlay != expected_runtime.get('incar')
            or bound.get('checks', {}).get('preparation_incar') is not True
            or not preparation.get('source')):
        raise ValueError('Changed native INCAR needs the exact approved preparation/runtime overlay')
    record = sources.json(preparation['source']['resolved_path'], 'study_preparation')
    if record.get('prepared_inputs') != expected_inputs or record.get('expected_runtime') != expected_runtime:
        raise ValueError('Preparation does not match all reviewed inputs and runtime settings')
    original = {}
    directory = Path(record['publication']['input_path'])
    for name in INPUT_NAMES:
        observed = sources.observe(directory / name, 'prepared_input:' + name)
        if observed['sha256'] != expected_inputs[name]['sha256']:
            raise ValueError('Original prepared input changed: ' + name)
        original[name] = (directory / name).read_bytes()
    comparison = compare_inputs(original, files, baseline_runtime=expected_runtime, prepared_runtime=expected_runtime)
    allowed = comparison['complete'] and all(
        item['category'] == 'title_comment_format' or
        (item['category'] == 'runtime_parallelization' and item.get('field') in overlay
         and item.get('after') == overlay[item['field']]) for item in comparison['changes'])
    if not allowed:
        raise ValueError('Native INCAR differs beyond the explicit reviewed runtime overlay')


def inspect_case(directory, *, policy_path, spec_path, expected_target, expected_inputs,
                 metric, expected_policy_id, expected_runtime=None, finalization_path=None):
    """Observe one previously joined case on its owner, retaining separate domains.

    The service must first check the original job's immutable study/case binding.
    This function independently checks global execution identity and every input
    byte against the reviewed case. A displayed energy never implies acceptance.
    """
    result = {'status': 'UNASSESSED', 'target': expected_target, 'metric': metric,
              'domains': {key: 'UNKNOWN' for key in ('process', 'completeness', 'native', 'policy', 'binding')},
              'finalization': 'NOT ASSESSED', 'energy': None, 'semantics': None,
              'sources': [], 'reasons': [], 'protected_directories': [str(directory)]}
    if not isinstance(metric, str) or metric not in ENERGIES:
        raise ValueError('Unsupported VASP energy definition')
    if set(expected_inputs) != set(INPUT_NAMES):
        raise ValueError('All four reviewed case input identities are required')
    sources = _Sources(MAX_INPUT_BYTES, None)
    try:
        root = Path(directory)
        if not root.is_absolute() or root.resolve() != root:
            raise ValueError('Scientific evidence needs its original canonical owner path')
        if finalization_path is not None:
            verified = verify_finalization(finalization_path, policy_path=policy_path)
            result['finalization'] = _status(verified['valid'])
            if verified['valid'] is not True:
                result['reasons'].extend(verified['findings'])
                return result
            record = verified['record']
            evidence = record['evidence']
            if Path(evidence['run_directory']) != root:
                raise ValueError('Finalized result belongs to a different run directory')
            if spec_path is not None:
                selected = sources.observe(spec_path, 'selected_finalization_spec')
                declarations = [s for s in evidence['binding']['sources'] if s['role'] == 'finalization_spec']
                if len(declarations) != 1 or selected['resolved_path'] != declarations[0]['resolved_path'] or selected['sha256'] != declarations[0]['sha256']:
                    raise ValueError('Selected binding differs from finalized result binding')
            result['record_id'] = record['record_id']
            native_sources = [verified['source'], *record['required_sources']]
        else:
            evidence = inspect_with_policy(root, policy_path=policy_path, spec_path=spec_path,
                                           max_bytes=MAX_INPUT_BYTES)
            binding = evidence.get('binding') or {}
            native_sources = [*evidence.get('sources', ()), evidence['policy_source'],
                              *binding.get('sources', ())]
        result['snapshot_id'] = evidence.get('snapshot_id')
        current = evidence.get('selected_segment') or {}
        coverage = evidence.get('coverage') or {}
        result['domains']['completeness'] = _status(bool(coverage.get('all_sources_complete')
            and coverage.get('all_sources_stable') and coverage.get('full_electronic_history')))
        result['native_mode'] = evidence.get('mode')
        evaluations = current.get('evaluations') or []
        result['domains']['native'] = ('UNSUPPORTED' if not evidence.get('mode', {}).get('supported')
            or evidence['mode'].get('calculation') != 'static' else
            'FAIL' if any(e.get('native_convergence') is False or e.get('numeric_convergence') == 'FAIL' for e in evaluations) else
            'PASS' if evaluations and all(e.get('native_convergence') is True and e.get('numeric_convergence') == 'PASS' for e in evaluations) else 'UNKNOWN')
        assessment = evidence.get('policy_assessment') or {}
        result['policy_identity'] = assessment.get('policy_identity')
        result['domains']['policy'] = assessment.get('status', 'NOT ASSESSED')
        if stable_hash(evidence.get('policy')) != expected_policy_id:
            raise ValueError('Observed scientific policy differs from the approved study policy')
        bound = evidence.get('binding') or {}
        execution = bound.get('execution') or {}
        result['domains']['process'] = _status(execution.get('operational_success'))
        execution_sources = {s['role']: s['resolved_path'] for s in execution.get('sources', ())}
        checked_execution = inspect_execution(execution_sources.get('jobs_snapshot'), execution_sources.get('jobs_receipt'),
            job_id=expected_target['job_id'], attempt_id=expected_target['attempt_id'],
            run_directory=root, runner_record=bound.get('resolved_spec', {}).get('execution', {}).get('runner_record'), native_sources=evidence.get('sources', ()),
            expected_target=expected_target)
        result['process'] = {k: checked_execution.get(k) for k in ('status', 'exit_code', 'signal', 'checks')}
        result['domains']['process'] = _status(checked_execution.get('operational_success'))
        if (checked_execution.get('checks', {}).get('owner_target') is not True
                and checked_execution['status'] != 'unverifiable'):
            raise ValueError('Result does not match the exact original global attempt')
        for observation in (checked_execution, execution):
            if observation.get('status') == 'unverifiable':
                result['reasons'].extend(item['message'] for item in observation['findings'])
                return result
        if execution.get('binding', {}).get('state_directory'):
            result['protected_directories'].append(execution['binding']['state_directory'])
        files = {}
        for name in INPUT_NAMES:
            identity = sources.observe(root / name, 'study_input:' + name)
            if identity['sha256'] != expected_inputs[name].get('sha256') and name != 'INCAR':
                raise ValueError('Scientific input differs from the reviewed case: ' + name)
            files[name] = (root / name).read_bytes()
        actual_incar = sources.records[str(root / 'INCAR')]['sha256']
        if actual_incar != expected_inputs['INCAR'].get('sha256'):
            _verify_runtime_incar(bound, files, expected_inputs, expected_runtime, sources)
        checked = check_input_bytes(files)
        if checked['status'] != 'valid':
            raise ValueError('Observed input bundle is outside the supported checker dialect')
        effective = {k: v['value'] for k, v in current.get('settings', {}).items()}
        if expected_runtime is not None:
            overlay = expected_runtime.get('incar')
            declared_overlay = bound.get('spec', {}).get('expected_runtime_overlay', {})
            if (not isinstance(overlay, dict) or set(overlay) - {'NCORE', 'KPAR'}
                    or any(type(value) is not int or value <= 0 or effective.get(tag) != value
                           for tag, value in overlay.items())
                    or any(overlay.get(tag) != value for tag, value in declared_overlay.items())):
                raise ValueError('Effective runtime controls differ from the approved study runtime')
        result['semantics'] = _input_semantics(files, checked, effective)
        result['input_identities'] = checked['identities']
        result['domains']['binding'] = _status(bound.get('input_binding') is True
            and bound.get('input_comparison_valid') is True
            and all(value is True for key, value in bound.get('checks', {}).items() if key != 'execution'))
        if result['semantics']['unknown_tags']:
            result['domains']['binding'] = 'UNASSESSED'
            result['reasons'].append('Unknown INCAR tags prevent complete semantic comparison')
        endpoint = evidence.get('endpoint') or {}
        index = endpoint.get('evaluation_index')
        if type(index) is int and 0 <= index < len(evaluations):
            energy = evaluations[index].get('energies', {}).get(metric)
            if energy and energy.get('units') == 'eV' and all(type(energy.get(k)) in (int, float)
                    and math.isfinite(energy[k]) for k in ('value', 'half_last_place')):
                result['energy'] = dict(energy, evaluation_index=index)
        if result['energy'] is None:
            result['reasons'].append('Selected evaluated energy is unavailable or has unsupported units/precision')
        if not revalidate_sources(native_sources)['valid']:
            raise ValueError('Scientific or binding sources changed during comparison')
        sources.verify_stable()
        result['sources'] = [{k: s.get(k) for k in ('role', 'resolved_path', 'sha256', 'bytes_read')}
            for s in [*native_sources, *sources.records.values()]]
        result['status'] = 'QUALIFIED' if result['energy'] and all(v == 'PASS' for v in result['domains'].values()) else 'UNASSESSED'
    except (OSError, ValueError, KeyError, TypeError) as error:
        result['domains']['binding'] = 'FAIL'
        result['reasons'].append(str(error))
    return result


def _compare_semantics(reference, observed, varied):
    reasons, changes = [], []
    a, b = reference.get('semantics'), observed.get('semantics')
    if not a or not b:
        return [], ['Required input semantics are unavailable']
    if a['unknown_tags'] or b['unknown_tags']:
        reasons.append('Unknown input tags leave semantic equivalence unassessed')
    if not a['charge'].get('NELECT') or not b['charge'].get('NELECT') or not a['spin'].get('ISPIN') or not b['spin'].get('ISPIN'):
        reasons.append('Effective charge or spin evidence is missing')
    for category in ('geometry', 'cell', 'composition', 'species_order', 'constraints', 'charge', 'spin',
                     'Hamiltonian', 'potential_bytes', 'k_sampling', 'numerical_controls'):
        before, after = a[category], b[category]
        if category == 'numerical_controls':
            for tag in sorted(set(before) | set(after)):
                if before.get(tag) != after.get(tag):
                    declared = tag == 'ENCUT' and 'ENCUT' in varied
                    changes.append({'category': 'ENCUT' if tag == 'ENCUT' else category, 'field': tag,
                                    'before': before.get(tag), 'after': after.get(tag), 'declared': declared})
                    if not declared:
                        reasons.append('Undeclared numerical control difference: ' + tag)
        elif before != after:
            declared = category in varied
            changes.append({'category': category, 'before': before, 'after': after, 'declared': declared})
            if category in {'geometry', 'cell', 'composition', 'species_order', 'constraints'}:
                reasons.append('Different ' + category + ': automatic energy deltas are unsupported')
            elif not declared or category not in {'k_sampling'}:
                reasons.append('Required invariant differs: ' + category)
    return changes, reasons


def compare_study(plan, rows):
    """Preserve every declared case and report only the selected absolute rule."""
    cases = plan['cases']
    if not 1 <= len(cases) <= MAX_CASES:
        raise ValueError('Comparison requires 1 to 64 explicit cases')
    case_ids = [case['case_id'] for case in cases]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError('Study case identities must be unique')
    metric, normalization, rule = (plan[key] for key in ('metric', 'normalization', 'comparison_rule'))
    reference_id = rule['reference_case']
    if reference_id not in case_ids:
        raise ValueError('Reference must name one declared case identity')
    by_case = {}
    for row in rows:
        if row.get('case_id') not in case_ids or row['case_id'] in by_case:
            raise ValueError('Result rows must name distinct declared cases')
        by_case[row['case_id']] = row
    result = {'schema': 1, 'kind': 'cmw-study-comparison', 'study_id': plan['study_id'],
              'study_digest': plan.get('digest'), 'metric': metric, 'normalization': normalization,
              'comparison_rule': rule, 'policy': plan.get('scientific_policy', plan.get('policy')),
              'delta_unit': 'eV/atom' if normalization['kind'] == 'per_atom' else 'eV',
              'rows': [], 'protected_directories': [], 'limitations': [
                  'Rule results apply only to the explicitly selected metric, reference and tolerance.',
                  'Matching settings do not establish equivalent physical or magnetic states, a ground state, or model accuracy.',
                  'Printed precision intervals describe rounding only, not all scientific uncertainty.',
                  'Different structures/compositions are side-by-side only; per_atom does not establish their comparability.']}
    reference = by_case.get(reference_id, {})
    for case in cases:
        row = dict(by_case.get(case['case_id'], {'status': 'MISSING', 'reasons': ['No result selected for this case']}))
        row.update(case_id=case['case_id'], name=case['name'], variables=case.get('variables'), delta=None,
                   delta_interval=None, rule_result='UNASSESSED', comparable=False)
        row['reasons'] = list(row.get('reasons', ()))
        if row.get('reason'):
            row['reasons'].append(str(row['reason']))
        result['protected_directories'].extend(row.get('protected_directories', ()))
        qualified = (row.get('status') == 'QUALIFIED' and reference.get('status') == 'QUALIFIED'
            and all(item.get('domains', {}).get(domain) == 'PASS' for item in (row, reference)
                    for domain in ('process', 'completeness', 'native', 'policy', 'binding')))
        if row.get('metric') not in (None, metric['name']) or reference.get('metric') not in (None, metric['name']):
            qualified = False
            row['reasons'].append('Energy definitions differ')
        changes, reasons = _compare_semantics(reference, row, set(plan['varied']))
        row['changes'] = changes
        row['reasons'].extend(reasons)
        if not qualified:
            row['reasons'].append('Case and reference each require separately qualified evidence')
        elif not reasons:
            energy, ref_energy = row['energy'], reference['energy']
            if energy.get('units') != 'eV' or ref_energy.get('units') != 'eV':
                row['reasons'].append('Unsupported or different energy units')
            else:
                count = sum(row['semantics']['composition'].values()) if normalization['kind'] == 'per_atom' else 1
                value, ref = (Decimal(v['printed'].replace('D', 'E').replace('d', 'e')) for v in (energy, ref_energy))
                error = (Decimal(str(energy['half_last_place'])) + Decimal(str(ref_energy['half_last_place']))) / count
                delta = (value - ref) / count
                if row['case_id'] == reference_id:
                    delta, error = Decimal(0), Decimal(0)
                tolerance = Decimal(str(rule['tolerance']))
                low, high = max(Decimal(0), abs(delta) - error), abs(delta) + error
                row.update(comparable=True, delta=float(delta), delta_interval=[float(delta-error), float(delta+error)],
                    rule_result='PASS' if high <= tolerance else 'FAIL' if low > tolerance else 'UNKNOWN')
        result['rows'].append(row)
    values = [row['rule_result'] for row in result['rows']]
    result['rule_result'] = ('FAIL' if 'FAIL' in values else 'PASS' if all(v == 'PASS' for v in values)
        else 'UNASSESSED' if 'UNASSESSED' in values else 'UNKNOWN')
    result['coverage'] = {status: sum(row.get('status') == status for row in result['rows'])
                          for status in sorted({row.get('status', 'UNKNOWN') for row in result['rows']})}
    result['case_count'] = len(cases)
    result['qualified_count'] = sum(row.get('status') == 'QUALIFIED' for row in result['rows'])
    result['protected_directories'] = sorted(set(result['protected_directories']))
    return result


def _csv_text(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r', '\n')) or value.startswith(('\t', '\r', '\n')) else value


def _markdown(value):
    return str(value).replace('\\', '\\\\').replace('|', '\\|').replace('\n', ' ').replace('\r', ' ').replace('<', '&lt;').replace('>', '&gt;')


def export_comparison(result, directory):
    """Write one new three-format report; existing locations are never replaced."""
    path = Path(directory)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError('Export requires a new canonical absolute directory')
    for protected in result.get('protected_directories', ()):
        if path.is_relative_to(Path(protected)):
            raise ValueError('Export must be outside scientific inputs/outputs and Jobs state')
    path.mkdir(mode=0o700)
    fields = ('case_id', 'name', 'status', 'process', 'completeness', 'native', 'policy', 'binding',
              'metric', 'unit', 'value', 'delta', 'rule_result', 'reasons',
              'study_digest', 'target', 'policy_identity', 'normalization', 'delta_unit', 'source_identities')
    csv_stream = io.StringIO(newline='')
    writer = csv.writer(csv_stream)
    writer.writerow(fields)
    lines = ['# Explicit convergence-study comparison', '',
             'Metric: ' + _markdown(result['metric']['name']) + ' (' + _markdown(result['metric']['unit']) + ').',
             'Rule result: ' + result['rule_result'] + '. This is the selected rule only.',
             'Study digest: ' + _markdown(result.get('study_digest')) + '.',
             'Reference: ' + _markdown(result['comparison_rule']['reference_case']) + '; absolute tolerance: '
             + str(result['comparison_rule']['tolerance']) + ' ' + result['delta_unit'] + '.',
             'Normalization: ' + result['normalization']['kind'] + '; delta units: ' + result['delta_unit'] + '.', '',
             '| Case | Evidence | Energy | Delta | Rule | Reasons |', '|---|---|---:|---:|---|---|']
    for row in result['rows']:
        domains, energy = row.get('domains', {}), row.get('energy') or {}
        values = [row['case_id'], row['name'], row['status'], *(domains.get(k, 'UNKNOWN') for k in fields[3:8]),
                  result['metric']['name'], energy.get('units', 'eV'), energy.get('value'), row['delta'],
                  row['rule_result'], '; '.join(row['reasons']), result.get('study_digest'),
                  json.dumps(row.get('target'), sort_keys=True), json.dumps(row.get('policy_identity'), sort_keys=True),
                  result['normalization']['kind'], result['delta_unit'], json.dumps(row.get('sources', []), sort_keys=True)]
        writer.writerow([_csv_text(v) if isinstance(v, str) else v for v in values])
        lines.append('| ' + ' | '.join(_markdown(v) for v in (row['name'], row['status'], energy.get('value'),
            row['delta'], row['rule_result'], '; '.join(row['reasons']))) + ' |')
    lines.extend(['', *('- ' + _markdown(note) for note in result['limitations']), '', '## Recorded provenance'])
    for row in result['rows']:
        lines.extend(['', '- Case: ' + _markdown(row['case_id']),
            '  - Target: ' + _markdown(json.dumps(row.get('target'), sort_keys=True)),
            '  - Policy: ' + _markdown(json.dumps(row.get('policy_identity'), sort_keys=True))])
        lines.extend('  - Source: ' + _markdown(source.get('resolved_path')) + ' / SHA-256 '
            + _markdown(source.get('sha256')) for source in row.get('sources', []))
    payloads = {'comparison.json': json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + '\n',
                'comparison.csv': csv_stream.getvalue(), 'comparison.md': '\n'.join(lines) + '\n'}
    try:
        for name, content in payloads.items():
            with (path / name).open('x', encoding='utf-8', newline='') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
    except Exception as error:
        with (path / 'INCOMPLETE').open('x', encoding='utf-8') as stream:
            stream.write('Report publication incomplete: ' + type(error).__name__ + '\n')
        raise
    return {name.rsplit('.', 1)[-1]: str(path / name) for name in payloads}
