"""Read-free Multiwfn descriptors shared by local and node observation views."""
from .models import CardCell, CardGroup


PROFILES = {'FMO': 'HOMO/LUMO cubes', 'ESP': 'Density/ESP cubes',
            'IGMH': 'Interfragment IGMH', 'IFCT': 'Two-fragment IFCT'}


def multiwfn_card_groups(snapshot):
    """Describe analysis scope without inferring an operation from file contents."""
    profile = snapshot.calculation or snapshot.context.stage
    if profile is None:
        profile = next((source.stage for source in snapshot.context.sources if source.stage), None)
    values = {setting.name: setting.value for setting in snapshot.settings}
    scope = []
    state = values.get('state_index')
    if type(state) is int and state > 0:
        scope.append(f'State {state}')
    threads = values.get('threads')
    if type(threads) is int and threads > 0:
        scope.append(f'{threads} threads')
    fragments = values.get('fragment_labels')
    if fragments:
        scope.append(f'{len(str(fragments).split(", "))} frags')
    analysis = PROFILES.get(profile, 'Profile unavailable')
    if values.get('spin_manifold'):
        analysis += ' · ' + str(values['spin_manifold'])
    native = str(values.get('native_stage') or '—').removeprefix('Native ')
    runner = str(values.get('runner_stage') or '—')
    roles = str(values.get('expected_roles') or 'Output roles unavailable')
    if snapshot.scientific_validation == 'validator-recorded-passed':
        output = 'Validator PASSED (recorded)'
    elif 'validation failed' in runner.lower():
        output = 'Validation failed (runner)'
    elif native != '—' or 'Process exited' in runner:
        output = 'Unvalidated'
    else:
        output = 'Pending evidence'
    return (
        CardGroup('ANALYSIS', ((CardCell(kind='text', text=analysis),
                                CardCell(kind='text', text=' · '.join(scope) or 'Scope unavailable')),)),
        CardGroup('STAGE', ((CardCell(kind='text', text='Native: ' + native),
                             CardCell(kind='text', text='Runner: ' + runner)),)),
        CardGroup('OUTPUT', ((CardCell(kind='text', text=roles),
                              CardCell(kind='text', text=output)),)),
    )
