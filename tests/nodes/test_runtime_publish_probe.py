"""Pure probe-oracle checks; no enrollment, native mutation or endpoint launch."""
from copy import deepcopy
import json
import unittest

from cmw.nodes.runtime_observation import RuntimeRefusal
from tests.nodes.runtime_publish_probe import failure_record, verify_lost_reply


class LostReplyOracleTests(unittest.TestCase):
    def setUp(self):
        self.request_id, self.digest, self.attempt = 'a' * 32, 'd' * 64, 'b' * 32
        job = {'id': 1, 'attempt_id': self.attempt,
               'node_submission': {'request_id': self.request_id, 'digest': self.digest}}
        self.error = {'code': 'OUTCOME_UNKNOWN'}
        self.report = {'mode': 'lost-submit-reply', 'committed_registration_before_lost_reply': {
            'status': 'REGISTERED', 'replayed': False, 'request_id': self.request_id, 'job': job}}
        self.recovered = {'request_id': self.request_id, 'operation': 'submit',
                          'digest': self.digest, 'result': deepcopy(job)}

    def verify(self, *, error=None, report=None, recovered=None):
        return verify_lost_reply(self.error if error is None else error,
            self.report if report is None else report,
            self.recovered if recovered is None else recovered,
            request_id=self.request_id, digest=self.digest)

    def test_exact_committed_lost_reply_and_recovered_attempt_pass(self):
        self.assertEqual(self.verify(), self.attempt)

    def test_other_transport_error_or_absent_injection_never_pass(self):
        with self.assertRaises(AssertionError):
            self.verify(error={'code': 'RUNTIME_MISMATCH'})
        with self.assertRaises(AssertionError):
            self.verify(report={'mode': 'lost-submit-reply'})

    def test_changed_request_digest_or_recovered_attempt_refuses(self):
        for field, value in (('request_id', 'c' * 32), ('digest', 'e' * 64),
                             ('operation', 'control')):
            with self.subTest(recovered_field=field), self.assertRaises(AssertionError):
                self.verify(recovered={**self.recovered, field: value})
        for field, value in (('id', 2), ('attempt_id', 'c' * 32)):
            with self.subTest(job_field=field), self.assertRaises(AssertionError):
                changed = deepcopy(self.recovered)
                changed['result'][field] = value
                self.verify(recovered=changed)
        for field, value in (('request_id', 'c' * 32), ('replayed', True)):
            with self.subTest(committed_field=field), self.assertRaises(AssertionError):
                changed = deepcopy(self.report)
                changed['committed_registration_before_lost_reply'][field] = value
                self.verify(report=changed)
        for field, value in (('request_id', 'c' * 32), ('digest', 'e' * 64)):
            with self.subTest(binding_field=field), self.assertRaises(AssertionError):
                changed_report, changed_recovered = deepcopy(self.report), deepcopy(self.recovered)
                changed_report['committed_registration_before_lost_reply']['job']['node_submission'][field] = value
                changed_recovered['result']['node_submission'][field] = value
                self.verify(report=changed_report, recovered=changed_recovered)


class SetupFailureEvidenceTests(unittest.TestCase):
    def test_setup_refusal_retains_structured_evidence_in_saved_json(self):
        failure = RuntimeRefusal('in_flight_change', phase='recollection-after-read',
            before={'member.py': [1, 2]}, after={'member.py': [1, 3]}, path='/fixture/member.py')
        try:
            raise failure
        except RuntimeRefusal as exc:
            result = failure_record(exc)
        saved = json.loads(json.dumps(result))
        self.assertEqual(saved['type'], 'RuntimeRefusal')
        self.assertEqual(saved['evidence'], failure.evidence)
        self.assertIn('recollection-after-read', json.dumps(saved['evidence']))
        self.assertIn('RuntimeRefusal', saved['traceback'])

    def test_ordinary_setup_error_does_not_invent_runtime_evidence(self):
        result = failure_record(ValueError('fixture failure'))
        self.assertNotIn('evidence', result)
        self.assertEqual(result['message'], 'fixture failure')
