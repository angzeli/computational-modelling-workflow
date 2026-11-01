"""Strict scientific wire round trips preserve evidence and accepted rendering."""
from copy import deepcopy
from dataclasses import replace
import json
import unittest

from cmw.nodes.codec import CodecError, decode_typed, encode_typed
from cmw.progress.formatting import progress_text
from cmw.progress.models import (HistoryColumn, HistoryEntry, HistoryPage, IterationLevel,
    ProgressSnapshot, RunContext, SourceDescriptor)
from cmw.jobs.source_selection import SourceFingerprint, SessionSelection
from tests.progress.test_electronic_notation import electronic_example


class ScientificCodecTests(unittest.TestCase):
    def test_full_scientific_fields_and_border_formatting_survive_wire(self):
        snapshot = electronic_example(calculation='fixed-cell-relaxation')
        payload = json.loads(json.dumps(encode_typed(snapshot)))
        decoded = decode_typed(payload, ProgressSnapshot)
        self.assertEqual(decoded, snapshot)
        self.assertTrue(any(cell.value_format for group in decoded.card_groups for row in group.rows for cell in row))
        for width in (70, 73, 93, 133):
            self.assertEqual(progress_text(decoded, width=width, now=112, max_lines=8, framed=True),
                             progress_text(snapshot, width=width, now=112, max_lines=8, framed=True))

    def test_history_nested_context_and_metadata_round_trip(self):
        context = RunContext('identity', 'VASP', (SourceDescriptor('stdout', '/node/output'),))
        page = HistoryPage(context, entries=(HistoryEntry('E7', 7,
            context=(IterationLevel('ionic', 7, 'Eval', 20),), annotations=('node-local evidence',)),),
            columns=(HistoryColumn('eval', 'EVAL'),), boundaries=(('relax', 1),), source_revision='generation')
        self.assertEqual(decode_typed(encode_typed(page), HistoryPage), page)

    def test_source_session_identity_tuples_remain_immutable(self):
        value = SessionSelection(('/node/queue', 1, 2, 3, 'attempt', 'VASP'),
            (SourceDescriptor('stdout', '/output'),),
            (SourceFingerprint('/output', '/output', (1, 2), (1, 3), 'AVAILABLE', ''),), 1, 'base')
        self.assertEqual(decode_typed(encode_typed(value), SessionSelection), value)

    def test_unknown_envelope_and_model_fields_refused(self):
        original = encode_typed(electronic_example())
        changes = [dict(original, extra=True), dict(original, schema=2), dict(original, schema=True),
                   dict(original, type='subprocess.Popen')]
        extra = deepcopy(original)
        extra['value']['invented'] = 'unexpected'
        changes.append(extra)
        missing = deepcopy(original)
        del missing['value']['card_groups']
        changes.append(missing)
        for value in changes:
            with self.subTest(value=value.keys()), self.assertRaises(CodecError):
                decode_typed(value)

    def test_wrong_scalar_types_and_nonfinite_numbers_refused(self):
        for key, value in (('segment', True), ('segment', '1'), ('status', 1),
                           ('last_success_at', float('nan')), ('last_success_at', float('inf'))):
            payload = encode_typed(electronic_example())
            payload['value'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(CodecError):
                decode_typed(payload)

    def test_wrong_nested_tuple_shape_refused(self):
        payload = encode_typed(electronic_example())
        payload['value']['card_groups'][0]['rows'] = {'metric': 'not a tuple'}
        with self.assertRaises(CodecError):
            decode_typed(payload)

    def test_wrong_response_type_refused(self):
        with self.assertRaises(CodecError):
            decode_typed(encode_typed(electronic_example()), HistoryPage)

    def test_model_validation_survives_decode(self):
        snapshot = electronic_example()
        series = snapshot.metrics[0]
        previous = replace(series.latest, semantic_id='different')
        payload = encode_typed(snapshot)
        payload['value']['metrics'][0]['previous'] = encode_typed(previous)['value']
        with self.assertRaises(CodecError):
            decode_typed(payload)

    def test_bounded_strings_sequences_and_depth(self):
        payload = encode_typed(RunContext('identity', 'VASP'))
        payload['value']['binding_id'] = 'x' * (256 * 1024 + 1)
        with self.assertRaises(CodecError):
            decode_typed(payload)
        payload = encode_typed(RunContext('identity', 'VASP'))
        payload['value']['limitations'] = ['x'] * 4097
        with self.assertRaises(CodecError):
            decode_typed(payload)
        nested = 0
        for _ in range(30):
            nested = [nested]
        session = SessionSelection((), (), (), 1, 'base')
        payload = encode_typed(session)
        payload['value']['identity'] = nested
        with self.assertRaises(CodecError):
            decode_typed(payload)

    def test_encoder_refuses_nonfinite_or_unregistered_objects(self):
        with self.assertRaises(CodecError):
            encode_typed(replace(electronic_example(), last_success_at=float('nan')))
        with self.assertRaises(CodecError):
            encode_typed({'arbitrary': 'object'})
