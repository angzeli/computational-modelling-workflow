"""Core-only compact formatting for bounded study observations."""
import json
from .snapshot import safe_snapshot_text


def study_text(record):
    """Show distinct result qualifications without implying scientific acceptance."""
    payload = json.loads(record.payload_json)
    lines = [f'STUDY · {record.study_id} · {record.status}',
             f'Owner: {record.node_id} / {record.queue_id}',
             'Plan: ' + record.plan_digest, '']
    if payload.get('message'):
        lines.append(str(payload['message']))
    lines.append('Read only · no preparation, registration or scheduling.')
    lines.append('')
    cases = payload.get('cases', [])
    if type(cases) is not list:
        cases = []
    for item in cases:
        if type(item) is not dict:
            continue
        name = str(item.get('case_id', item.get('name', 'case')))
        target = item.get('target') or item.get('attempt') or {}
        if type(target) is dict and target.get('job_id'):
            name += f' · J{target["job_id"]} · {target.get("attempt_id", "unbound")}'
        lines.append(name)
        if item.get('name') and item['name'] != item.get('case_id'):
            lines.append('  '+str(item['name']))
        for key in ('variables', 'status', 'process', 'completeness', 'native_convergence', 'policy', 'binding', 'metric', 'value', 'delta', 'comparability', 'reason'):
            if key in item:
                value = item[key]
                rendered = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
                lines.append(f'  {key.replace("_", " ")}: {rendered}')
    if not cases:
        lines.append('No bound study cases available.')
    for key in ('metric', 'normalization', 'coverage', 'verdict', 'limitations'):
        if key in payload:
            value = payload[key]
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
            lines.append(f'{key.title()}: {rendered}')
    lines.extend(('', 'Selected results remain pinned to recorded immutable attempts.',
                  'T Catalogue / M Series members and R Review retain their existing meanings.'))
    return safe_snapshot_text('\n'.join(lines))

