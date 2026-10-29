"""Prospective coverage-based visual inventory; historical failures stay failed."""
from __future__ import annotations

import time

SIZES = ((140, 40), (100, 30), (77, 24), (80, 24))
MACHINE_HEADER = 'Machine: CPU 71%; avg 63°C RAM 27.1 / 36.0 GiB'


def frame_id(frontend, family, size):
    return f'{frontend}-{family}-{size[0]}x{size[1]}'


def plan():
    core = [{'id': frame_id(view, family, size), 'frontend': view, 'family': family, 'size': list(size)}
            for view in ('local', 'nodes') for family in ('static', 'relaxation', 'multiwfn') for size in SIZES]
    modals = [{'id': frame_id(view, modal, (80, 24)), 'frontend': view, 'family': modal, 'size': [80, 24]}
              for view in ('local', 'nodes') for modal in ('review', 'catalogue', 'study', 'doctor')]
    return {'schema': 2, 'core': core, 'modals': modals,
            'required_settled_count': 32, 'synthetic_machine_header': MACHINE_HEADER,
            'resize_sequence': [[140, 40], [100, 30], [77, 24], [80, 24], [140, 40]],
            'retained_assertions': ['immutable target/attempt', 'deliberate offscreen cursor',
                'horizontal offset', 'nested modal return', 'stale restoration cancellation',
                'late progress/card height', 'row append/reorder', 'unavailable-owner isolation',
                'post-settlement and later normal refresh', 'missing-attempt refusal',
                'VASP electronic notation and force decimals', 'ORCA native units', 'Multiwfn stage continuity'],
            'historical_mapping': {
                'original local/federated native resize': '16 static/relaxation settled views plus all resize transition captures and geometry assertions',
                'extra analysis resize': '8 Multiwfn settled views plus all resize transition captures and native literal assertions',
                'extra catalogue/study/review/doctor resize-page-wheel': '8 modal80x24 settled views; all size/page/wheel transitions retain geometry/literal/identity assertions and transition captures',
                'review frozen text and copy refresh': 'retain distinct transition captures and body/identity/native mouse refresh assertions',
                'active writer navigation': 'retain interaction trace, geometry/target assertions and transition/failure captures',
                'original 294 missing exports': 'historical BLOCKED unchanged; no retrospective acceptance'},
            'visual_review': 'Inspect every required settled image and every failure image; missing images block visual_result.'}


def synthetic_machine():
    # A rendering fixture only; independent resource accounting observes real processes.
    from tests.jobs.test_machine_thermal_ui import machine
    return machine(now=time.time(), age=0.)


class SyntheticMachineSampler:
    def sample(self, *args, **kwargs):
        return {'machine_usage': synthetic_machine(), 'jobs': {}, 'external': {}}

    def close(self):
        pass
