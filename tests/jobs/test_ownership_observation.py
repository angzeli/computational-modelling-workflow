"""Control authority with kernel v2 and pre-existing v1 owner records."""
import errno
import json
import unittest
from unittest.mock import MagicMock, patch

import psutil

from cmw.jobs import ownership


BOOT = 'a0dc8d4e-c809-4717-9dbe-c4b2fbece908'
V2 = {'version': 2, 'host': 'local', 'boot_session': BOOT, 'pid': 123, 'start_token': 111}
V1 = {'host': 'local', 'boot': 1793633892.0, 'pid': 123, 'birth': 1793654971.378596}


class OwnershipObservationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(ownership.socket, 'gethostname', return_value='local'))
        self.boot = self.enterContext(patch.object(ownership, 'boot_session_id', return_value=BOOT))
        self.process = self.enterContext(patch.object(ownership.process_identity, 'process_evidence',
                                                    return_value=(111, False)))

    def observed(self, record, status, name):
        result = ownership.observe_owner(record)
        self.assertEqual((result.status, result.deciding_check), (status, name))
        self.assertEqual(json.loads(json.dumps(result.as_dict()))['status'], status)
        return result.checks

    def test_same_boot_session_and_start_token_survive_wall_clock_step_and_psutil_adjustment(self):
        with patch.object(psutil, 'boot_time', return_value=1793662684.0) as wall_boot, \
                patch.object(psutil, 'Process', side_effect=AssertionError('adjusted birth unavailable')):
            self.observed(V2, 'VERIFIED', 'identity')
        wall_boot.assert_not_called()

    def test_same_pid_different_token_confirms_reuse(self):
        self.process.return_value = (112, False)
        check = self.observed(V2, 'GONE', 'start_token')['start_token']
        self.assertEqual((check['recorded'], check['observed']), (111, 112))

    def test_different_boot_session_confirms_original_is_gone(self):
        self.boot.return_value = '041ec104-f68b-4e22-82ba-c630db0a7b17'
        self.observed(V2, 'GONE', 'boot_session')
        self.process.assert_not_called()

    def test_v1_incident_boot_and_adjusted_birth_cannot_prove_loss(self):
        process = MagicMock()
        process.create_time.return_value = 1793683763.378596
        with patch.object(psutil, 'Process', return_value=process) as find, \
                patch.object(psutil, 'boot_time', return_value=1793662684.0) as wall_boot:
            checks = self.observed(V1, 'UNVERIFIABLE', 'boot')
            find.assert_not_called()
            self.assertEqual(checks['boot']['observed'], 1793662684.0)
            wall_boot.return_value = V1['boot']
            checks = self.observed(V1, 'UNVERIFIABLE', 'birth')
            self.assertEqual(checks['birth']['recorded'], 1793654971.378596)
            self.assertEqual(checks['birth']['observed'], 1793683763.378596)

    def test_missing_host_evidence_and_other_host_never_probe_pid(self):
        for host in ('elsewhere', ''):
            with self.subTest(host=host):
                self.assertEqual(ownership.observe_owner({**V2, 'host': host}).status, 'UNVERIFIABLE')
        self.process.assert_not_called()

    def test_boot_error_with_esrch_is_not_process_absence(self):
        self.boot.side_effect = ProcessLookupError(errno.ESRCH, 'boot read failed')
        self.observed(V2, 'UNVERIFIABLE', 'boot_session')
        self.process.assert_not_called()

    def test_confirmed_absence_is_distinct_from_access_failure_and_unexpected_exception(self):
        for error, verdict, check in [(ProcessLookupError(errno.ESRCH, 'absent'), 'GONE', 'pid'),
                                       (PermissionError(), 'UNVERIFIABLE', 'start_token'),
                                       (OSError(errno.EIO, 'read error'), 'UNVERIFIABLE', 'start_token'),
                                       (RuntimeError('unavailable'), 'UNVERIFIABLE', 'start_token')]:
            with self.subTest(error=error):
                self.process.side_effect = error
                self.observed(V2, verdict, check)

    def test_zombie_still_occupies_pid_but_cannot_receive_control(self):
        self.process.return_value = (111, True)
        self.observed(V2, 'UNVERIFIABLE', 'process_status')
        self.assertFalse(ownership.owner_alive(V2))

    def test_boolean_retries_only_unverifiable_reads(self):
        for probes, expected, delays in [
            ([(111, False)], True, []),
            ([(112, False)], False, []),
            ([PermissionError(), (111, False)], True, []),
            ([PermissionError(), (112, False)], False, []),
            ([PermissionError(), PermissionError(), (111, False)], True, [0.01]),
            ([PermissionError()] * 3, False, [0.01]),
        ]:
            with self.subTest(probes=probes), patch.object(ownership, 'time') as clock:
                clock.monotonic.return_value = 1.
                self.process.reset_mock()
                self.process.side_effect = probes
                self.assertIs(ownership.owner_alive(V2), expected)
                self.assertEqual(self.process.call_count, len(probes))
                self.assertEqual([call.args[0] for call in clock.sleep.call_args_list], delays)

    def test_boolean_starts_no_read_after_its_retry_deadline(self):
        for times, expected_reads in [([0., .2], 1), ([0., 0., 0., .2], 2)]:
            with self.subTest(times=times), patch.object(ownership, 'time') as clock:
                clock.monotonic.side_effect = times
                self.process.reset_mock()
                self.process.side_effect = PermissionError()
                self.assertIs(ownership.owner_alive(V2), False)
                self.assertEqual(self.process.call_count, expected_reads)

    def test_tri_state_still_returns_the_first_denial(self):
        self.process.side_effect = [PermissionError(), (111, False)]
        self.observed(V2, 'UNVERIFIABLE', 'start_token')
        self.process.assert_called_once_with(V2['pid'])

    def test_partial_and_unknown_records_cannot_verify(self):
        for record in [None, {}, {**V2, 'version': 3}, {**V2, 'start_token': True},
                       {**V2, 'boot_session': ''}, {**V1, 'birth': float('nan')}]:
            with self.subTest(record=record):
                self.observed(record, 'UNVERIFIABLE', 'record')
        self.process.assert_not_called()

    def test_existing_matching_v1_record_verifies_even_if_boot_uuid_is_unavailable(self):
        process = MagicMock()
        process.create_time.return_value = V1['birth']
        process.status.return_value = psutil.STATUS_RUNNING
        self.boot.side_effect = PermissionError()
        with patch.object(psutil, 'boot_time', return_value=V1['boot']), \
                patch.object(psutil, 'Process', return_value=process):
            self.observed(V1, 'VERIFIED', 'identity')
            self.assertNotIn('version', V1)
