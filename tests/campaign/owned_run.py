"""Run one final command with task-owned launch, sampling and cleanup evidence.

Example (test-only driver import path must already be explicit):
    python -m tests.campaign.owned_run --timeout 2400 --output NEW_DIRECTORY -- COMMAND ARG...

The direct command is recorded at Popen launch. Grandchildren are first-observed
owned descendants; internal lifecycle manifests can supply their earlier births.
This driver does not claim continuous short-lived descendant observation.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import cmw

from tests.campaign.support import BUDGETS, OBSERVATION_RULES, ProcessLedger, summary
from tests.campaign.qualification_v2 import outcomes


def run(argv, output, timeout, *, max_owned=BUDGETS['owned_subprocesses']):
    output = Path(output).resolve()
    output.mkdir(mode=0o700)
    report = {'schema': 1, 'argv': list(argv), 'timeout_seconds': timeout,
              'python': sys.executable, 'package_origin': cmw.__file__,
              'max_sampled_owned_subprocesses': max_owned,
              'observation_rules': OBSERVATION_RULES, 'samples': [], 'sampling_errors': []}
    stopped = threading.Event()
    ledger = ProcessLedger('command:' + Path(argv[0]).name)
    started = time.monotonic()
    process = None
    final_sample = None
    with ledger:
        def sample():
            while not stopped.wait(OBSERVATION_RULES['sample_cadence_seconds']):
                try:
                    report['samples'].append(ledger.sample())
                except BaseException as exc:
                    report['sampling_errors'].append(repr(exc))
                    return
        sampler = threading.Thread(target=sample, name='owned-run-resource-sampler', daemon=True)
        try:
            report['samples'].append(ledger.sample())
            sampler.start()
            ledger.phase = 'command'
            with (output/'command.log').open('wb') as log:
                process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                                           stderr=subprocess.STDOUT, close_fds=True)
                report['returncode'] = process.wait(timeout=timeout)
        except BaseException as exc:
            report['error'] = type(exc).__name__ + ': ' + str(exc)
        finally:
            workload_ended = time.monotonic()
            report['workload_seconds'] = workload_ended-started
            cleanup_deadline = workload_ended + OBSERVATION_RULES['cleanup_seconds']
            stopped.set()
            if sampler.ident is not None:
                sampler.join(timeout=min(2., max(0., cleanup_deadline-time.monotonic())))
            try:
                report['remaining'] = ledger.cleanup(cleanup_deadline)
            except BaseException as exc:
                report['cleanup_error'] = repr(exc)
            finally:
                stopped.set()
                if sampler.ident is not None:
                    sampler.join(timeout=min(2., max(0., cleanup_deadline-time.monotonic())))
            try:
                final_sample = ledger.sample()
                report['samples'].append(final_sample)
            except BaseException as exc:
                report['sampling_errors'].append('Final resource observation: '+repr(exc))
            report['sampler_stopped'] = not sampler.is_alive()
            report['cleanup_seconds'] = time.monotonic()-workload_ended
            if process is not None and process.poll() is not None:
                report['reaped_returncode'] = process.wait(timeout=0)
            report['ownership'] = ledger.report()
    report['peak_sampled_owned_subprocesses'] = max(
        (sum(row['pid'] != os.getpid() for row in sample['processes']) for sample in report['samples']), default=0)
    report['sample_cadence_seconds'] = summary([after['at']-before['at']
        for before, after in zip(report['samples'], report['samples'][1:])])
    resources = [row for sample in report['samples'] for row in sample['processes']]
    def frontend(sample):
        return next((row for row in (sample or {}).get('processes', ())
                     if all(row[key] == ledger.frontend[key] for key in ('pid', 'birth'))), None)
    initial_frontend = frontend(report['samples'][0] if report['samples'] else None)
    final_frontend = frontend(final_sample)
    growth = final_frontend['fds']-initial_frontend['fds'] if initial_frontend and final_frontend and 'fds' in initial_frontend and 'fds' in final_frontend else None
    report['resource_peaks'] = {'rss_bytes': max((row['rss_bytes'] for row in resources if 'rss_bytes' in row), default=None),
                               'fds': max((row['fds'] for row in resources if 'fds' in row), default=None)}
    report['frontend_descriptors'] = {'initial': initial_frontend, 'final': final_frontend, 'growth': growth}
    report['resource_limits'] = {key: BUDGETS[key] for key in
                                ('process_rss_bytes', 'process_fds', 'frontend_fd_growth')}
    violations = []
    if not resources:
        violations.append('No successful resource observations')
    if any(('rss_bytes' in row and row['rss_bytes'] > BUDGETS['process_rss_bytes']) for row in resources):
        violations.append('Per-process RSS budget exceeded')
    if any(('fds' in row and row['fds'] > BUDGETS['process_fds']) for row in resources):
        violations.append('Per-process FD budget exceeded')
    if growth is None:
        violations.append('Frontend initial/final descriptor observation missing')
    elif growth > BUDGETS['frontend_fd_growth']:
        violations.append('Frontend descriptor growth budget exceeded')
    report['resource_budget_violations'] = violations
    report['resource_limits_passed'] = not violations
    observation = report['ownership']
    if report['sampling_errors']:
        observation.update(observation_accounting='FAIL', resource_observability='RESOURCE_QUALIFICATION_INCOMPLETE')
    report['process_exits'] = {'command_returncode': report.get('returncode'),
                              'command_reaped_returncode': report.get('reaped_returncode')}
    report['outcomes'] = outcomes(functional_passed=report.get('returncode') == 0 and 'error' not in report,
        safety_passed=observation['control_identity_safety'] == 'PASS',
        cleanup_passed='cleanup_error' not in report and not report.get('remaining', [])
            and not observation['cleanup_errors'] and report['sampler_stopped']
            and report['cleanup_seconds'] <= OBSERVATION_RULES['cleanup_seconds'],
        observation=observation, resource_limits_passed=report['resource_limits_passed']
            and report['peak_sampled_owned_subprocesses'] <= max_owned)
    report['passed'] = report['outcomes']['qualified']
    report['execution_passed'] = report['outcomes']['execution_passed']
    report['harness_returncode'] = int(not report['passed'])
    (output/'result.json').write_text(json.dumps(report, indent=2)+'\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=float, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-owned', type=int, default=BUDGETS['owned_subprocesses'])
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.command[1:] if args.command[:1] == ['--'] else args.command
    if args.timeout <= 0 or args.max_owned < 1 or not argv:
        parser.error('Provide positive bounds and a command after --')
    result = run(argv, args.output, args.timeout, max_owned=args.max_owned)
    print(json.dumps({'passed': result['passed'], 'report': str(args.output/'result.json'),
                      'workload_seconds': result['workload_seconds']}))
    raise SystemExit(not result['passed'])


if __name__ == '__main__':
    main()
