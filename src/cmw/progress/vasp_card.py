"""VASP presentation descriptors; no numerical inference or file reading."""
from .models import CardCell, CardGroup


def vasp_card_groups(snapshot):
    labels = (('evaluation', 'E'), ('electronic', 'SCF'))
    electronic = {'context_kinds': ('electronic',), 'context_labels': labels}
    force_ids = ('vasp.force.maximum_atomic_norm.free', 'vasp.force.maximum_atomic_norm.all')
    settings = {item.name: item for item in snapshot.settings if item.qualification != 'other-segment'}
    target = 'Target unavailable'
    if snapshot.calculation == 'static':
        target = 'Target N/A · static'
    elif 'EDIFFG' in settings:
        value = settings['EDIFFG'].value
        if isinstance(value, (int, float)) and value > 0:
            target = 'Target N/A · ionic energy'
        elif value == 0:
            target = 'Target N/A · EDIFFG=0'
        if target != 'Target unavailable' and settings['EDIFFG'].qualification == 'provisional':
            target += '*'
    energy_ids = ('vasp.electronic.dE', 'vasp.electronic.d_eps')
    energy_target = 'EDIFF unavailable'
    if 'EDIFF' in settings and settings['EDIFF'].value == 0:
        energy_target = 'EDIFF=0 · fixed iterations'
        if settings['EDIFF'].qualification == 'provisional':
            energy_target += '*'
    evaluation = next((level for level in snapshot.iteration if level.kind == 'evaluation'), None)
    evaluation_text = evaluation.label.replace('evaluation', 'Eval', 1) if evaluation else 'Eval unassigned'
    return (
        CardGroup('SCF', ((CardCell(kind='text', text=evaluation_text),
                           CardCell(kind='context', context_kinds=('electronic',), text='SCF unavailable')),)),
        CardGroup('FORCE', ((CardCell(force_ids, text='— (not observed)',
                                      context_kinds=('evaluation',), context_labels=labels),
                             CardCell(force_ids, kind='target', text=target)),)),
        CardGroup('ELECTRONIC', (
            (CardCell((energy_ids[0],), '|dE|', **electronic),
             CardCell((energy_ids[1],), '|d eps|', **electronic)),
            (CardCell(('vasp.electronic.rms',), 'rms', **electronic),
             CardCell(energy_ids, 'EDIFF (both)', kind='target', text=energy_target)),
        )),
    )
