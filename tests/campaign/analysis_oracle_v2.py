"""Separate full IFCT fixture evidence from width-limited painted stage cells."""
from __future__ import annotations

import re


def contains(text, token):
    return token in text or ''.join(token.split()) in ''.join(text.split())


def analysis_evidence(snapshot, case):
    """Verify complete fixture literals and immutable identity before display checks."""
    assert snapshot is not None and snapshot.context.engine == 'Multiwfn'
    assert snapshot.context.attempt_id == case['attempt_id']
    assert not snapshot.metrics, 'Analysis fixture must not invent SCF/geometry metrics'
    assert snapshot.calculation == case['profile']
    assert [group.label for group in snapshot.card_groups] == ['ANALYSIS', 'STAGE', 'OUTPUT']
    cells = [cell.text for group in snapshot.card_groups for row in group.rows for cell in row]
    assert all(contains('\n'.join(cells), token) for token in case['literal_text']), {
        'expected': case['literal_text'], 'full_descriptor_cells': cells}
    settings = {item.name: item.value for item in snapshot.settings}
    stages = {'Native': str(settings.get('native_stage', '')).removeprefix('Native '),
              'Runner': str(settings.get('runner_stage', ''))}
    assert all(stages.values()), stages
    assert all(any(token in value for token in case['literal_text']) for value in stages.values()), stages
    if 'Validator PASSED (recorded)' in case['literal_text']:
        assert snapshot.scientific_validation == 'validator-recorded-passed'
    return {'attempt_id': snapshot.context.attempt_id, 'profile': snapshot.calculation,
            'full_literals': case['literal_text'], 'stages': stages,
            'scientific_validation': snapshot.scientific_validation}


def analysis_painted(snapshot, case, painted_lines):
    """Permit only an explicit stage-cell ellipsis; scope/output remain exact."""
    evidence = analysis_evidence(snapshot, case)
    painted = '\n'.join(painted_lines)
    stage_tokens = {token for token in case['literal_text']
                    if any(token in value for value in evidence['stages'].values())}
    exact = ['ANALYSIS', 'STAGE', 'OUTPUT',
             *(token for token in case['literal_text'] if token not in stage_tokens)]
    assert all(contains(painted, token) for token in exact), {'expected': exact, 'painted': painted}
    rows = [line for line in painted_lines if 'STAGE' in line]
    assert len(rows) == 1, {'stage_rows': rows}
    row = rows[0]
    if '│' in row:
        cells = [cell.strip() for cell in row.split('│') if cell.strip()]
    else:
        cells = re.split(r' {2,}', row.strip())
    assert len(cells) == 3 and cells[0] == 'STAGE', {'stage_cells': cells}
    rendering = []
    for (label, value), shown in zip(evidence['stages'].items(), cells[1:]):
        full = label + ': ' + value
        truncated = shown.endswith('…')
        # Require a substantial, exact field prefix and visible truncation marker.
        # A missing field, arbitrary shortened word, or wrong stage never passes.
        assert shown == full or (truncated and len(shown[:-1]) >= 20
                                 and full.startswith(shown[:-1]) and len(shown) < len(full)), {
            'field': label, 'full': full, 'painted': shown}
        rendering.append({'field': label, 'full': full, 'painted': shown,
                          'display': 'explicit-ellipsis' if truncated else 'complete'})
    return {**evidence, 'painted_exact_literals': exact, 'stage_rendering': rendering}


def analysis_details(evidence, body):
    """The real Details body must retain both full stages and the same attempt."""
    expected = [evidence['attempt_id'], *evidence['stages'].values()]
    assert all(contains(body, token) for token in expected), {'expected': expected, 'body': body}
    return {'full_literals': expected, 'validation': 'actual-details-model-body',
            'body': body, 'painted_full_stage_claimed': False}
