"""Both final comparison branches retain the actual operands, not later stats."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from cmw.nodes import identity
from cmw.nodes.runtime_observation import RuntimeRefusal, file_epoch, inventory
from tests.nodes import test_runtime_observation as fixtures
from tests.nodes.runtime_publish_probe import failure_record


class CollectionEvidenceTests(unittest.TestCase):
    setUp = fixtures.RuntimeObservationTests.setUp

    def observations(self):
        first = inventory(self.package)
        name, original = first[1][0]
        changed = list(original)
        changed[4] += 1
        current = (first[0], ((name, tuple(changed)),), first[2])
        executable = file_epoch(Path(sys.executable).resolve())
        return first, current, executable

    def collect(self, first, current, after, executable, executable_after):
        failure = RuntimeRefusal('in_flight_change', phase='before-open', path='__init__.py',
                                 before=first[1][0][1], after=current[1][0][1])
        with patch.object(identity, 'inventory', side_effect=[first, current, after]), \
             patch.object(identity, 'file_epoch', side_effect=[executable, executable, executable_after, executable_after]), \
             patch.object(identity, '_package_digest', side_effect=failure) as digest, \
             patch.object(identity.sys, 'platform', 'darwin'):
            digest.__wrapped__ = Mock(return_value=self.original['package_sha256'])
            return identity.runtime_observed()

    def test_inventory_failure_retains_root_members_directories_and_equal_interpreter(self):
        first, current, executable = self.observations()
        after = deepcopy(current)
        changed = list(after[1][0][1]); changed[4] += 1
        after = (after[0], (('__init__.py', tuple(changed)),), after[2])
        with self.assertRaises(RuntimeRefusal) as caught:
            self.collect(first, current, after, executable, executable)
        saved = json.loads(json.dumps(failure_record(caught.exception)))['evidence']
        self.assertEqual(saved['phase'], 'recollection-after-read')
        self.assertTrue(saved['comparison']['inventory_changed'])
        self.assertFalse(saved['comparison']['executable_changed'])
        self.assertEqual(saved['before']['members']['__init__.py'], list(current[1][0][1]))
        self.assertEqual(saved['after']['members']['__init__.py'], changed)
        self.assertEqual(saved['before']['root'], list(current[0]))
        self.assertEqual(saved['after']['directories'], after[2])
        self.assertEqual(saved['before']['executable'], saved['after']['executable'])
        self.assertEqual((saved['collector_pass'], saved['recovery_count']), (2, 1))
        self.assertEqual(saved['first_refusal']['phase'], 'before-open')
        self.assertEqual(saved['actual_digest'], self.original['package_sha256'])

    def test_interpreter_failure_retains_equal_inventory_and_both_target_epochs(self):
        first, current, executable = self.observations()
        changed = list(executable); changed[4] += 1
        with self.assertRaises(RuntimeRefusal) as caught:
            self.collect(first, current, current, executable, tuple(changed))
        saved = json.loads(json.dumps(failure_record(caught.exception)))['evidence']
        self.assertFalse(saved['comparison']['inventory_changed'])
        self.assertTrue(saved['comparison']['executable_changed'])
        for key in ('root', 'members', 'directories'):
            self.assertEqual(saved['before'][key], saved['after'][key])
        self.assertEqual(saved['before']['executable'], list(executable))
        self.assertEqual(saved['after']['executable'], changed)
        self.assertEqual(saved['comparison']['executable_lexical'], sys.executable)
        self.assertEqual(saved['comparison']['executable_resolved'], str(Path(sys.executable).resolve()))

    def test_coherent_recollection_compares_current_not_obsolete_first_inventory(self):
        first, current, executable = self.observations()
        result, observed = self.collect(first, current, current, executable, executable)
        self.assertEqual(result['fingerprint'], self.original['fingerprint'])
        self.assertEqual(observed['members']['__init__.py'], list(current[1][0][1]))
