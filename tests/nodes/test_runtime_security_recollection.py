"""A classified native security race permits one disk reread, not new trust."""
from contextlib import contextmanager
from copy import deepcopy
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from cmw.nodes import identity, native_security
from cmw.nodes.runtime_observation import RuntimeRefusal, RuntimeVerifier, inventory
from tests.nodes import test_runtime_observation as fixtures


@unittest.skipUnless(sys.platform == 'darwin', 'Darwin native ACL and UF_TRACKED contract')
class RuntimeSecurityRecollectionTests(unittest.TestCase):
    def setUp(self):
        fixtures.RuntimeObservationTests.setUp(self)
        self.security.stop()
        identity._package_digest.cache_clear()
        self.original, self.epoch = identity.runtime_observed()
        self.addCleanup(identity._package_digest.cache_clear)

    @contextmanager
    def after_native_acl(self, *, reads=(2,)):
        """Change only the fixture member after a successful native ACL read."""
        original = native_security.acl_evidence
        member_id = (self.member.stat().st_dev, self.member.stat().st_ino)
        count, events = 0, []

        def observe(descriptor, **options):
            nonlocal count
            result = original(descriptor, **options)
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) == member_id:
                count += 1
                if count in reads:
                    before = native_security.object_epoch(info)
                    os.chflags(self.member, info.st_flags ^ 0x40)
                    events.append({'before': before,
                                   'after': native_security.object_epoch(self.member.stat()),
                                   'acl': result})
            return result

        identity._package_digest.cache_clear()
        with patch.object(native_security, 'acl_evidence', side_effect=observe):
            yield events

    def eligible_evidence(self):
        first = inventory(self.package)
        member = dict(first[1])['__init__.py']
        before = tuple(member[index] for index in (0, 1, 5, 6, 7, 9, 2, 3, 4, 8))
        after = list(before)
        after[8] += 1
        after[9] ^= 0x40
        evidence = RuntimeRefusal('in_flight_change', phase='security-after-read',
            path=str(self.member), before=before,
            after={'descriptor': tuple(after), 'path': tuple(after),
                   'acl': {'state': 'absent', 'api': 'fstatx_np/filesec_query_property', 'errno': 0}},
            actual_digest=member[-2], api='fstat/lstat', security_state='complete',
            object_type='file').evidence
        return first, evidence

    def test_native_post_acl_change_recollects_once_with_complete_diagnostics(self):
        diagnostics = []
        with self.after_native_acl() as events:
            record, observed = identity.runtime_observed(diagnostics=diagnostics)
        self.assertEqual(len(events), 1)
        self.assertIn(events[0]['acl']['state'], {'absent', 'present'})
        self.assertEqual(events[0]['before'][:8], events[0]['after'][:8])
        self.assertEqual(events[0]['before'][9] ^ events[0]['after'][9], 0x40)
        self.assertEqual(record['fingerprint'], self.original['fingerprint'])
        self.assertEqual(record['package_sha256'], self.original['package_sha256'])
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]['attempts'], 2)
        self.assertEqual(diagnostics[0]['outcome'], 'COHERENT_CURRENT_DISK_ONLY')
        refusal = diagnostics[0]['first_refusal']
        self.assertEqual(refusal['phase'], 'security-after-read')
        self.assertEqual(refusal['security_state'], 'complete')
        self.assertEqual(tuple(refusal['before']), events[0]['before'])
        self.assertEqual(tuple(refusal['after']['descriptor']), events[0]['after'])
        self.assertEqual(refusal['after']['descriptor'], refusal['after']['path'])
        self.assertEqual(refusal['actual_digest'], self.epoch['members']['__init__.py'][-2])
        self.assertNotEqual(observed, self.epoch)

    def test_recollection_does_not_make_an_old_observer_fresh(self):
        verifier = RuntimeVerifier(self.original, self.epoch, process_started_ns=0, isolated=True)
        with self.after_native_acl():
            with self.assertRaises(RuntimeRefusal) as caught:
                verifier.verify(identity.runtime_observed)
        self.assertEqual(caught.exception.evidence['category'], 'fresh_isolated_observer_required')
        self.assertFalse(verifier.recovered)
        self.assertEqual(verifier.current, self.epoch)

    def test_synthetic_fresh_observer_retains_original_trust(self):
        trusted = deepcopy(self.epoch)
        verifier = RuntimeVerifier(self.original, self.epoch,
                                   process_started_ns=10**20, isolated=True)
        with self.after_native_acl():
            result = verifier.verify(identity.runtime_observed)
        self.assertEqual(result, self.original)
        self.assertEqual(verifier.original, trusted)
        self.assertTrue(verifier.recovered)
        self.assertEqual(verifier.last_evidence['category'], 'uf_tracked_current_disk_verified')

    def test_initial_inventory_race_has_no_complete_baseline_and_does_not_retry(self):
        diagnostics = []
        with self.after_native_acl(reads=(1,)) as events:
            with patch.object(identity, '_package_digest', wraps=identity._package_digest) as digest:
                with self.assertRaises(RuntimeRefusal) as caught:
                    identity.runtime_observed(diagnostics=diagnostics)
        self.assertEqual(caught.exception.evidence['phase'], 'security-after-read')
        self.assertEqual(len(events), 1)
        self.assertEqual(diagnostics, [])
        digest.assert_not_called()

    def test_second_native_security_race_remains_a_refusal(self):
        diagnostics = []
        with self.after_native_acl(reads=(2, 4)) as events:
            with self.assertRaises(RuntimeRefusal) as caught:
                identity.runtime_observed(diagnostics=diagnostics)
        self.assertEqual(len(events), 2)
        self.assertEqual(caught.exception.evidence['phase'], 'security-after-read')
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]['outcome'], 'REFUSED')

    def test_only_original_member_complete_classified_epochs_are_eligible(self):
        first, evidence = self.eligible_evidence()
        classify = identity._metadata_only_security_refusal
        self.assertTrue(classify(evidence, first, self.package))
        for index in range(8):
            with self.subTest(object_epoch_field=index):
                invalid = deepcopy(evidence)
                after = list(invalid['after']['descriptor'])
                after[index] += 1
                invalid['after']['descriptor'] = invalid['after']['path'] = tuple(after)
                self.assertFalse(classify(invalid, first, self.package))
        for flags in (0x02, 0x42):
            with self.subTest(flags=flags):
                invalid = deepcopy(evidence)
                after = list(invalid['after']['descriptor'])
                after[9] = invalid['before'][9] ^ flags
                invalid['after']['descriptor'] = invalid['after']['path'] = tuple(after)
                self.assertFalse(classify(invalid, first, self.package))
        for path in (self.package, self.package / 'absent.py', Path(sys.executable).resolve()):
            with self.subTest(path=path):
                self.assertFalse(classify({**evidence, 'path': str(path)}, first, self.package))
        for phase in ('security-open', 'acl-read', 'xattr-list', 'xattr-read'):
            with self.subTest(phase=phase):
                self.assertFalse(classify({**evidence, 'phase': phase}, first, self.package))
        for field in ('before', 'after', 'actual_digest', 'security_state', 'path'):
            with self.subTest(missing=field):
                invalid = deepcopy(evidence)
                invalid.pop(field)
                self.assertFalse(classify(invalid, first, self.package))
        for state in ('denied', 'unsupported', 'error', None):
            with self.subTest(security_state=state):
                self.assertFalse(classify({**evidence, 'security_state': state}, first, self.package))
        self.assertFalse(classify({**evidence, 'actual_digest': '0' * 64}, first, self.package))
        invalid = deepcopy(evidence)
        invalid['after']['path'] = invalid['before']
        self.assertFalse(classify(invalid, first, self.package))
        invalid = deepcopy(evidence)
        before = list(invalid['before'])
        before[1] += 1
        invalid['before'] = tuple(before)
        self.assertFalse(classify(invalid, first, self.package))
        incomplete = deepcopy(first)
        name, member = incomplete[1][0]
        incomplete = (incomplete[0], ((name, (*member[:-1], False)),), incomplete[2])
        self.assertFalse(classify(evidence, incomplete, self.package))
        with patch.object(identity.sys, 'platform', 'linux'):
            self.assertFalse(classify(evidence, first, self.package))

    def test_unsafe_transient_evidence_cannot_hide_behind_another_member_delta(self):
        other = self.package / 'other.py'
        other.write_text('value=2\n')
        _, evidence = self.eligible_evidence()
        after = list(evidence['after']['descriptor'])
        after[2] ^= 0o020
        evidence['after']['descriptor'] = evidence['after']['path'] = tuple(after)
        refused = RuntimeRefusal('in_flight_change', phase='security-after-read')
        refused.evidence = evidence

        def fail_after_other_metadata_change(root, epoch):
            os.chflags(other, other.stat().st_flags ^ 0x40)
            raise refused

        retry = Mock(side_effect=AssertionError('Unsafe evidence must never reach recollection'))
        diagnostics = []
        with patch.object(identity, '_package_digest', side_effect=fail_after_other_metadata_change) as digest:
            digest.__wrapped__ = retry
            with self.assertRaises(RuntimeRefusal) as caught:
                identity.runtime_observed(diagnostics=diagnostics)
        self.assertIs(caught.exception, refused)
        self.assertEqual(diagnostics, [])
        retry.assert_not_called()
