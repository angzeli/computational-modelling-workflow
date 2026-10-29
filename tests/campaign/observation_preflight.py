"""Four-second, benign resource-observer calibration before Contract V2 freeze.

Two equal two-second windows use the same three stdin-gated Python helpers:
first no periodic sampling, then the existing 100 ms cadence. Roles are assigned
for coverage calibration only; these waiters are not functional CMW controllers.
The report never modifies thresholds or certifies continuous resource bounds.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import select
import subprocess
import sys
import threading
import time

from tests.campaign.qualification_v2 import PLAN
from tests.campaign.support import OBSERVATION_RULES, ProcessLedger, summary

WINDOW_SECONDS = 2.
ROLES = ('controller', 'endpoint', 'worker')


def run_case(sampled):
    samples, children, errors, remaining = [], [], [], []
    ledger = ProcessLedger('observation-preflight-' + ('sampled' if sampled else 'idle'))
    cpu_started = started = ended = cpu_ended = None
    with ledger:
        try:
            for role in ROLES:
                child = subprocess.Popen([sys.executable, '-I', '-B', '-c',
                    "import sys; print('ready', flush=True); sys.stdin.buffer.read(1)"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
                children.append(child)
                record, = [row for row in ledger.records if ledger.direct.get(row['ledger_id']) is child]
                record.update(role=role, role_evidence='explicit benign preflight waiter, not a CMW controller')
                readable, _, _ = select.select([child.stdout], [], [], 3.)
                assert readable and child.stdout.readline() == b'ready\n', 'Benign waiter setup did not acknowledge'
            started, cpu_started = time.monotonic(), time.process_time()
            deadline = started + WINDOW_SECONDS
            stopped = threading.Event()
            if sampled:
                while time.monotonic() < deadline:
                    samples.append(ledger.sample())
                    stopped.wait(min(OBSERVATION_RULES['sample_cadence_seconds'], max(0., deadline-time.monotonic())))
            else:
                stopped.wait(WINDOW_SECONDS)
            ended, cpu_ended = time.monotonic(), time.process_time()
        except BaseException as exc:
            errors.append({'stage': 'setup_or_body', 'type': type(exc).__name__, 'message': str(exc)})
        finally:
            ledger.finish_sampling()
            for child in children:
                try:
                    if child.poll() is None:
                        child.stdin.write(b'X'); child.stdin.flush()
                    child.stdin.close()
                    code = child.wait(timeout=3.)
                    if code != 0:
                        errors.append({'stage': 'payload', 'pid': child.pid, 'returncode': code})
                except (OSError, subprocess.SubprocessError) as exc:
                    errors.append({'stage': 'graceful_stop', 'pid': child.pid, 'error': repr(exc)})
                finally:
                    child.stdout.close(); child.stderr.close()
            try:
                remaining = ledger.cleanup(time.monotonic()+5.)
            except BaseException as exc:
                errors.append({'stage': 'cleanup', 'error': repr(exc)})
    return {'sampling_enabled': sampled, 'window_seconds': ended-started if ended is not None else None,
        'observer_process_cpu_seconds': cpu_ended-cpu_started if cpu_ended is not None else None,
        'observer_call_seconds': summary([sample['observer_seconds'] for sample in samples]),
        'observer_total_call_seconds': sum(sample['observer_seconds'] for sample in samples),
        'sampling_intervals_seconds': summary([right['at']-left['at'] for left, right in zip(samples, samples[1:])]),
        'samples': samples, 'ownership': ledger.report(), 'errors': errors, 'remaining': remaining,
        'resource_claim': 'sampled maxima only' if sampled else 'none: intentionally unsampled baseline'}


def run(output):
    output = Path(output).resolve(); output.parent.mkdir(parents=True, exist_ok=True)
    assert not output.exists(), 'Calibration output must be fresh'
    idle, sampled = run_case(False), run_case(True)
    observation = sampled['ownership']
    report = {'schema': 2, 'purpose': 'preflight calibration; not final qualification',
        'plan': PLAN, 'python': sys.executable, 'roles': ['frontend', *ROLES],
        'baseline': idle, 'sampled': sampled,
        'additional_observer_cpu_seconds': (sampled['observer_process_cpu_seconds']-idle['observer_process_cpu_seconds']
            if all(row['observer_process_cpu_seconds'] is not None for row in (idle, sampled)) else None),
        'observer_active_wall_fraction': (sampled['observer_total_call_seconds']/sampled['window_seconds']
            if sampled['window_seconds'] else None),
        'candidate_coverage_rules_met': observation['resource_observability'] == 'PASS',
        'cleanup_passed': all(not row['remaining'] and not row['errors'] and not row['ownership']['cleanup_errors']
                              for row in (idle, sampled)),
        'freeze_required': True}
    report['passed'] = report['candidate_coverage_rules_met'] and report['cleanup_passed']
    output.write_text(json.dumps(report, indent=2)+'\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    report = run(args.output)
    print(json.dumps({key: report[key] for key in ('passed', 'candidate_coverage_rules_met', 'cleanup_passed',
        'observer_active_wall_fraction', 'additional_observer_cpu_seconds', 'freeze_required')}))
    raise SystemExit(not report['passed'])


if __name__ == '__main__':
    main()
