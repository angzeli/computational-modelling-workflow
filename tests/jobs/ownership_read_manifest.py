"""Auditable read-boundary inventory and explicit sweep applicability.

The dynamic matrix contains every occurrence reached in the fifteen real-worker
fixtures. This inventory also records functions those fixtures cannot reach: a
read-only CLI, enrollment, a separate suspend/continue request, qualification,
and recovery-only paths are not silently counted as terminal lifecycle rows.
Their existing focused contract suites are named, without claiming that those
suites perform the complete three-errno cross-product.
"""
from __future__ import annotations

import ast
from pathlib import Path

from tests.jobs.ownership_fault_sweep import MODULES, READ_NAMES


# Prefixes are lexical scopes, not line numbers: moving code does not silently
# exclude it. A new function with a read needs an explicit classification.
FAMILIES = {
    'cmw.jobs.ownership': {
        'worker': ('identity', '_legacy_identity', 'identity_for', 'observe_owner',
                   'lock_held', 'group_members', '_snapshot_owned_session',
                   'signal_session', 'observe_group'),
        'contract': ('owner_alive', 'observed_group_members'),
        'effect': ('exclusive',),
    },
    'cmw.jobs.ownership_hold': {
        'worker': ('WorkerAuthority',),
        'contract': ('blocks_replacement',),
    },
    'cmw.jobs.suspension': {
        'worker': ('_file_identity', '_present', 'finalize_timing', 'Channel',
                   '_process_read', '_descends', '_observed_exit', '_check_identity',
                   '_check_runner', 'bind_domain', '_signal_witness', '_send', 'Coordinator'),
        'contract': ('enroll_state', '_alive_authority', '_storage', 'readiness',
                     'project_timing', '_submit', '_executable_matches', '_stopped'),
    },
    'cmw.jobs.suspension_protocol': {
        'worker': ('require_control_owners', 'require_control_session', 'clock_descriptor',
                   '_directory_identity', '_read_fd', 'RunnerControl'),
        'contract': ('now_clock', 'file_identity', '_mpi_configuration_files',
                     'mpi_runtime_identity', 'controlled_driver', 'main'),
    },
    'cmw.jobs.runtime': {
        'worker': ('runtime_environment', 'finish', '_wait_for_group_end', '_receipt',
                   '_read_job', '_commit_job', '_Completion', '_worker_intent', 'worker',
                   ),
        'recovery': ('_AdmissionRefusal', '_wait_unobserved_domain'),
        'contract': ('reap_detached', 'start', '_prepare_start', '_stop', 'mark_unknown',
                     'reconcile', 'launch_blocker', 'dispatch_authority', 'tick', 'controller'),
        'effect': ('detached',),
    },
    'cmw.molecular.orca.suspension': {
        'worker': ('_read_private_record', 'verify_completion'),
        'contract': ('_read_preparation', 'describe', 'write_completion', 'main'),
    },
}
CONTRACT_TESTS = {
    'cmw.jobs.ownership': ['tests/jobs/test_ownership_observation.py',
                         'tests/jobs/test_cancellation_ownership.py',
                         'tests/jobs/test_ownership_hold.py'],
    'cmw.jobs.ownership_hold': ['tests/jobs/test_ownership_hold.py'],
    'cmw.jobs.suspension': ['tests/jobs/test_suspension_state.py',
                          'tests/jobs/test_suspension_ownership.py'],
    'cmw.jobs.suspension_protocol': ['tests/jobs/test_suspension_protocol.py'],
    'cmw.jobs.runtime': ['tests/jobs/test_cancellation_anchor.py',
                       'tests/jobs/test_clock_freshness.py',
                       'tests/jobs/test_completion_retry.py'],
    'cmw.molecular.orca.suspension': ['tests/runtime/test_orca_suspension.py'],
}
LIMITS = [
    'Native processes and time advancement are modeled; no VASP, ORCA or real signal runs.',
    'The actual worker, SQLite queue, coordinator, runner protocol, native-reader '
    'normalization and ORCA completion verifier execute.',
    'Identity capture is supplied by the process model; native identity readers '
    'are separately exercised by test_process_identity and test_ownership_observation.',
    'Ancestry and descendant enumeration use the real raw-token/edge algorithms; '
    'only native parent/children/start evidence comes from the process model. '
    'Controlled fixtures include a child below the computational root.',
    'External-activity admission and controller dispatch authorization are fixture '
    'inputs; controller-only reads are not claimed as worker lifecycle coverage.',
    'Recovery-only reads require a prior fault and are covered by focused hold/complete '
    'contracts rather than credited to the single-fault fault-free-baseline sweep.',
    'A receipt arriving before the first verified coordinated domain has a '
    'separate accepted pending outcome: retain the live worker and pipe, refuse '
    'signals and RELEASE, and report attention after 60 seconds of virtual time.',
    'Suspend/continue, MPI qualification, enrollment, CLI and observation-only '
    'functions have distinct contracts and are not normal/cancel/timeout fixture paths.',
    'Worker death, durable recovery and controller completion adoption are PR B.',
]


def read_sites():
    """Return source inventory, rejecting newly unclassified read scopes."""
    rows = []
    for module in MODULES:
        path = Path(module.__file__)
        families = FAMILIES[module.__name__]

        class Visitor(ast.NodeVisitor):
            scopes = []

            def visit_FunctionDef(self, node):
                self.scopes.append(node.name)
                self.generic_visit(node)
                self.scopes.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_ClassDef(self, node):
                self.scopes.append(node.name)
                self.generic_visit(node)
                self.scopes.pop()

            def visit_Call(self, node):
                expression = ast.unparse(node.func)
                last = expression.split('.')[-1]
                if last not in READ_NAMES:
                    self.generic_visit(node)
                    return
                # Dictionary reads cannot have a native EAGAIN/EPERM outcome.
                if last == 'get' and expression not in {'store.get', 'self.store.get'}:
                    self.generic_visit(node)
                    return
                scope = '.'.join(self.scopes)
                candidates = [(category, prefix) for category, prefixes in families.items()
                              for prefix in prefixes
                              if scope == prefix or scope.startswith(prefix + '.')]
                if not candidates:
                    raise AssertionError(f'Unclassified read: {module.__name__}:{scope}:{node.lineno}:{expression}')
                category = max(candidates, key=lambda item: len(item[1]))[0]
                args = ast.unparse(node)
                # Open-for-write and nonzero signal delivery are effects, not
                # read-fault sites. The directory open before publication remains
                # a read and is exercised before the one-time write.
                if (last == 'open' and any(flag in args for flag in ('O_WRONLY', 'O_CREAT'))
                        or last == 'flock' and scope != 'lock_held'
                        or expression == 'os.killpg' and len(node.args) > 1
                        and not (isinstance(node.args[1], ast.Constant) and node.args[1].value == 0)):
                    category = 'effect'
                rows.append(dict(module=module.__name__, function=scope, line=node.lineno,
                                 end_line=node.end_lineno,
                                 call=expression, classification=category,
                                 contract_tests=CONTRACT_TESTS[module.__name__]))
                self.generic_visit(node)

        Visitor().visit(ast.parse(path.read_text()))
    return rows


def coverage_manifest(dynamic_manifest):
    """Keep lexical applicability separate from actually reached source reads."""
    observed = {}
    for lifecycle in dynamic_manifest:
        for point in lifecycle['baseline_reads']:
            scope = '.'.join(part for part in point['function'].split('.')
                             if not part.startswith('<'))
            key = point['module'], scope
            observed.setdefault(key, set()).add(point['line'])
    rows = read_sites()
    for row in rows:
        lines = observed.get((row['module'], row['function']), ())
        row['reached_in_fault_free_baseline'] = any(
            row['line'] <= line <= row['end_line'] for line in lines)
    return rows
