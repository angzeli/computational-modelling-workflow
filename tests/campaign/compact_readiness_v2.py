"""Actual compact PTY bodies over explicit three-row native-shaped fixtures.

Readiness only: no 1000/10000-row history, SSH, real engine or scientific acceptance.
The original 240-second qualification history contract is unchanged.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import time

from cmw.jobs.store import Store
from tests.campaign.closure_workflows import installed_origin
from tests.campaign.pty import run as local_terminal, compact_core
from tests.campaign.support import fixture, endpoint_command

WORK_SECONDS = 120


def run(directory):
    root = Path(directory).resolve()
    root.mkdir(mode=0o700)
    report = {'schema':2,'readiness_only':True,'large_history_qualified':False,
              'real_engines':False,'scientific_acceptance_claimed':False,
              'work_seconds':WORK_SECONDS,'status':'RUNNING','origin':installed_origin(),
              'fixtures':[],'stage':'fixture-setup'}
    started = time.monotonic()
    previous = signal.getsignal(signal.SIGALRM)
    def timeout(signum, frame):
        raise TimeoutError('Compact PTY readiness work deadline exceeded')
    signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, WORK_SECONDS)
    evidence = root/'runtime-refusals'
    evidence.mkdir(mode=0o700)
    previous_evidence = os.environ.get('CMW_TEST_EVIDENCE_DIR')
    os.environ['CMW_TEST_EVIDENCE_DIR'] = str(evidence)
    try:
        for count, family in ((1000,'static'),(10000,'relaxation')):
            record = fixture(root/family, count, history_rows=3)
            record['command'] = endpoint_command(record['entry']['config_root'])
            record['readiness_only'] = True
            state = Store(record['state']).snapshot()
            assert len(state['jobs']) == record['actual_history_rows'] == 3
            assert all(job['status']=='Hold' for job in state['jobs'])
            report['fixtures'].append(record)
        settings = root/'local-settings.json'
        settings.write_text(json.dumps({'view':'local','fixtures':report['fixtures']},indent=2)+'\n')
        report['stage'] = 'local-core'
        report['local'] = local_terminal(settings, root/'local-pty')
        report['stage'] = 'federated-core'
        (root/'nodes-pty').mkdir()
        report['nodes'] = compact_core({'view':'nodes','fixtures':report['fixtures']}, root/'nodes-pty')
        for record in report['fixtures']:
            state = Store(record['state']).snapshot()
            assert len(state['jobs']) == 3 and all(job['status']=='Hold' for job in state['jobs'])
            assert not state['controller']['online'] and not state['controller']['dispatch']
        report.update(status='PASS', stage='complete')
    except BaseException as exc:
        report.update(status='FAIL', error=type(exc).__name__+': '+str(exc))
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if previous_evidence is None:
            os.environ.pop('CMW_TEST_EVIDENCE_DIR',None)
        else:
            os.environ['CMW_TEST_EVIDENCE_DIR'] = previous_evidence
        report['elapsed_seconds'] = time.monotonic()-started
        if report['elapsed_seconds'] > WORK_SECONDS:
            report.update(status='FAIL', budget_exceeded=True)
        (root/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    assert report['status']=='PASS', report
    return report


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    run(parser.parse_args().directory)
