"""VASP presentation descriptors; no numerical inference or file reading."""
import math

from .models import CardCell, CardGroup


def _qualified_settings(snapshot):
    settings = {}
    for item in snapshot.settings:
        if item.qualification not in ('effective', 'provisional'):
            continue
        if item.name not in settings or settings[item.name].qualification != 'effective':
            settings[item.name] = item
    return settings


def _finite_value(setting):
    value = setting.value if setting else None
    return value if type(value) in (int, float) and math.isfinite(value) else None


def vasp_card_groups(snapshot):
    labels = (('evaluation', 'E'), ('electronic', 'SCF'))
    electronic = {'context_kinds': ('electronic',), 'context_labels': labels}
    force_ids = ('vasp.force.maximum_atomic_norm.free', 'vasp.force.maximum_atomic_norm.all')
    settings = _qualified_settings(snapshot)
    force_setting, energy_setting = settings.get('EDIFFG'), settings.get('EDIFF')
    force_value, energy_value = _finite_value(force_setting), _finite_value(energy_setting)
    target = 'Target unavailable'
    if snapshot.calculation == 'static':
        target = 'Target N/A · static'
    elif force_value is not None:
        if force_value > 0:
            target = 'Target N/A · ionic energy'
        elif force_value == 0:
            target = 'Target N/A · EDIFFG=0'
        elif snapshot.calculation == 'fixed-cell-relaxation' and snapshot.status != 'UNSUPPORTED':
            # The setting is known before a force/constraint comparison exists.
            target = f'Conditional <{-force_value:g} eV/Å'
        if target != 'Target unavailable' and force_setting.qualification == 'provisional':
            target += '*'
    energy_ids = ('vasp.electronic.dE', 'vasp.electronic.d_eps')
    energy_target = 'EDIFF unavailable'
    if energy_value is not None:
        if energy_value == 0:
            energy_target = 'EDIFF=0 · fixed iterations'
        elif energy_value > 0 and snapshot.status != 'UNSUPPORTED':
            energy_target = f'EDIFF (both) <{energy_value:g} eV'
        if energy_target != 'EDIFF unavailable' and energy_setting.qualification == 'provisional':
            energy_target += '*'
    evaluation = next((level for level in snapshot.iteration if level.kind == 'evaluation'), None)
    evaluation_text = f'Eval {evaluation.index}' if evaluation else 'Eval unassigned'
    # Only OUTCAR contexts include an evaluation; source-local context is electronic-only.
    native = next((level for level in snapshot.iteration if level.kind == 'electronic'), None) if evaluation else None
    scf_text = 'OUTCAR SCF unavailable'
    if native is not None and type(native.index) is int and native.index > 0:
        budget = native.budget
        valid_budget = (type(budget) is int and budget > 0 and not any(
            item.name == 'NELM' and item.qualification != 'effective' for item in snapshot.settings))
        scf_text = f'OUTCAR SCF {native.index}/' + (str(budget) if valid_budget else '—')
    return (
        CardGroup('SCF', ((CardCell(kind='text', text=evaluation_text),
                           CardCell(kind='text', text=scf_text)),)),
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
