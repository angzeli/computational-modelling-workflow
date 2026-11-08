"""Bounded cross-layer research invariants over disposable, engine-free state."""
import errno
import json
import os
from pathlib import Path
import random
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from cmw.doctor.service import inspect_local
from cmw.jobs.catalogue import CatalogueStore
from cmw.jobs.catalogue_models import CatalogueQuery
from cmw.jobs.catalogue_relations import RelationStore
from cmw.jobs.review import ReviewService
from cmw.jobs.store import JobsError, Store
from cmw.jobs.successor import SuccessorService
from tests.jobs.successor_fixtures import make_vasp_fixture, successor_case
from tests.periodic.vasp.test_result_execution_native import run_fixture


def trace(name, events, *, error=None):
    """Retain concrete reproducible operation prefixes only when requested."""
    destination = os.environ.get('CMW_RESEARCH_TRACE_DIR')
    if destination:
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        (root / (name + '.json')).write_text(json.dumps({
            'case': name, 'events': events, 'outcome': 'failed' if error else 'passed',
            'error': None if error is None else str(error),
            'scope': 'Synthetic fixture state; no scientific engine or production access',
        }, indent=2) + '\n')


class ResearchSequenceTests(unittest.TestCase):
    def test_seeded_catalogue_controls_and_readers_match_independent_state(self):
        for seed in (3, 17, 61):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory(prefix='cmw-research-sequence-') as tmp:
                root = Path(tmp).resolve()
                store = Store(root / 'state')
                store.enroll_queue(uuid4().hex, 'synthetic-research-host')
                jobs = [store.add(argv=['/unexecuted/synthetic'], cwd=root, hold=True,
                                  name='same literal.* name') for _ in range(3)]
                catalogue = CatalogueStore(store)
                reviews = ReviewService(store)
                expected = {job['id']: {'status': 'Hold', 'revision': 0, 'alias': '',
                                       'attempt_id': job['attempt_id']} for job in jobs}
                rng = random.Random(seed)
                operations = ['release', 'hold', 'edit', 'bad-control', 'bad-edit', 'read']
                sequence = operations + [rng.choice(operations) for _ in range(18)]
                events = [{'seed': seed, 'steps': len(sequence)}]
                try:
                    for index, operation in enumerate(sequence):
                        job = jobs[rng.randrange(len(jobs))]
                        model = expected[job['id']]
                        events.append({'step': index, 'operation': operation, 'job_id': job['id']})
                        if operation in {'hold', 'release'}:
                            store.change(job['id'], operation, expected_attempt_id=job['attempt_id'])
                            model['status'] = 'Hold' if operation == 'hold' else 'Queue'
                        elif operation == 'edit':
                            alias = f'seed-{seed}-step-{index}'
                            catalogue.edit(job['id'], {'alias': alias}, expected_revision=model['revision'],
                                           expected_attempt_id=job['attempt_id'])
                            model.update(alias=alias, revision=model['revision'] + 1)
                        else:
                            before = store.path.read_bytes()
                            if operation == 'bad-control':
                                with self.assertRaises(JobsError):
                                    store.change(job['id'], 'release', expected_attempt_id=uuid4().hex)
                            elif operation == 'bad-edit':
                                with self.assertRaises(JobsError):
                                    catalogue.edit(job['id'], {'project': 'forbidden'},
                                                   expected_revision=model['revision'] + 1,
                                                   expected_attempt_id=job['attempt_id'])
                            else:
                                # Doctor, Review and catalogue must all observe the same owner without writing.
                                inspect_local(store.root, job['id'], job['attempt_id'])
                                preview = reviews.preview(job['id'], job['attempt_id'])
                                self.assertTrue(all(domain.status == 'NOT ASSESSED' for domain in preview.domains))
                                catalogue.query(CatalogueQuery(text='literal.*'))
                            self.assertEqual(store.path.read_bytes(), before)
                        snapshot = store.snapshot()
                        observed = {item['id']: item['status'] for item in snapshot['jobs']}
                        self.assertEqual(observed, {key: value['status'] for key, value in expected.items()})
                        self.assertFalse(snapshot['controller']['online'])
                        page = catalogue.query(CatalogueQuery(text='literal.*'))
                        self.assertEqual(page.total_count, 3)
                        self.assertEqual(len(page.records), 3)
                        for row in page.records:
                            self.assertEqual({key: getattr(row, key) for key in expected[row.job_id]},
                                             expected[row.job_id])
                except Exception as exc:
                    trace(f'seed-{seed}', events, error=exc)
                    raise
                finally:
                    reviews.close()
                trace(f'seed-{seed}', events)

    def test_series_membership_never_substitutes_for_exact_attempt_relationship(self):
        with tempfile.TemporaryDirectory(prefix='cmw-research-join-') as tmp:
            root = Path(tmp).resolve()
            store = Store(root / 'state')
            owner = store.enroll_queue(uuid4().hex, 'synthetic-research-host')
            jobs = [store.add(argv=['/unexecuted/synthetic'], cwd=root, hold=True,
                              name='identical') for _ in range(3)]
            catalogue, relations = CatalogueStore(store), RelationStore(store)
            for job in jobs:
                catalogue.edit(job['id'], {'series': 'explicit-group', 'tags': ['same']},
                               expected_revision=0, expected_attempt_id=job['attempt_id'])
            parent = {key: owner[key] for key in ('node_id', 'queue_id')}
            parent.update(job_id=jobs[0]['id'], attempt_id=jobs[0]['attempt_id'])
            before = store.snapshot()['jobs']
            child = jobs[1]
            with self.assertRaises(JobsError):
                relations.add(child['id'], parent={**parent, 'attempt_id': jobs[2]['attempt_id']},
                              kind='next-stage', expected_revision=1, expected_attempt_id=child['attempt_id'])
            result = relations.add(child['id'], parent=parent, kind='next-stage',
                                   expected_revision=1, expected_attempt_id=child['attempt_id'])
            self.assertEqual(result.relations[0].parent, parent)
            self.assertEqual(result.relations[0].basis, 'intent')
            self.assertIn('does not satisfy', result.relations[0].qualification)
            remote = {**parent, 'node_id': uuid4().hex, 'queue_id': uuid4().hex}
            unresolved = relations.add(jobs[2]['id'], parent=remote, kind='comparison',
                                       expected_revision=1, expected_attempt_id=jobs[2]['attempt_id'])
            self.assertEqual(unresolved.relations[0].resolution, 'UNRESOLVED_REMOTE')
            self.assertEqual(store.snapshot()['jobs'], before)
            self.assertEqual(len(catalogue.query(CatalogueQuery(series='explicit-group')).records), 3)
            trace('exact-relationship', ['same names and series', 'wrong attempt refused',
                                       'exact explicit edge retained', 'remote edge unresolved', 'jobs unchanged'])


class ResearchNativeReviewTests(unittest.TestCase):
    def test_native_schemas_review_and_catalogue_read_without_promoting_acceptance(self):
        with tempfile.TemporaryDirectory(prefix='cmw-research-native-') as tmp:
            root = Path(tmp).resolve()
            for schema in (2, 3, 4):
                with self.subTest(schema=schema):
                    if schema == 4:
                        # The older native-review fixture uses a non-wire node label.
                        # Cross-layer catalogue checks need the real enrollment shape.
                        fixture = make_vasp_fixture(root / 'schema-4')
                        store, run = fixture['store'], fixture['run']
                        snapshot = store.snapshot()
                    else:
                        store, snapshot, _, run = run_fixture(root / f'schema-{schema}', schema)
                    job = snapshot['jobs'][0]
                    before = store.path.read_bytes()
                    source_bytes = {p.name: p.read_bytes() for p in run.iterdir() if p.is_file()}
                    service = ReviewService(store)
                    try:
                        inspect_local(store.root, job['id'], job['attempt_id'])
                        rows = CatalogueStore(store).query(CatalogueQuery()).records
                        self.assertEqual([row.attempt_id for row in rows], [job['attempt_id']])
                        record = service.start(job['id'], {}, f'schema-{schema}', job['attempt_id'])
                        deadline = time.monotonic() + 35
                        while record.status == 'PENDING' and time.monotonic() < deadline:
                            time.sleep(.05)
                            record = service.poll(job['id'], record.review_id, job['attempt_id'])
                        self.assertEqual(record.status, 'COMPLETE', record.message)
                        domains = {domain.name: domain.status for domain in record.domains}
                        self.assertEqual(domains['operational'], 'PASS')
                        self.assertEqual(domains['policy'], 'NOT ASSESSED')
                        self.assertEqual(domains['finalization'], 'NOT ASSESSED')
                        cached = service.preview(job['id'], job['attempt_id'])
                        self.assertEqual(cached.status, 'CACHED')
                        self.assertNotIn('PASS', [domain.status for domain in cached.domains])
                        self.assertEqual(store.path.read_bytes(), before)
                        self.assertEqual({p.name: p.read_bytes() for p in run.iterdir() if p.is_file()}, source_bytes)
                        trace(f'native-schema-{schema}', ['native benign terminal receipt', 'doctor and catalogue read',
                              {'domains': domains}, 'cache removes PASS', 'queue and scientific fixture bytes unchanged'])
                    finally:
                        service.close()


class ResearchSuccessorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-research-successor-')
        cls.addClassCleanup(temporary.cleanup)
        cls.fixture = make_vasp_fixture(Path(temporary.name) / 'owner')

    def setUp(self):
        self.store = self.fixture['store']
        self.parent = self.fixture['job']
        self.service = SuccessorService(self.store)
        self.spec, self.path = successor_case(self.fixture, uuid4().hex)

    def plan(self):
        return self.service.plan(self.parent['id'], str(self.path), self.parent['attempt_id'])

    def materialize(self, plan):
        return self.service.materialize(self.parent['id'], str(self.path), plan.plan_digest,
                                        self.parent['attempt_id'])

    def test_lost_registration_reply_survives_catalogue_edits_and_read_only_doctor(self):
        plan = self.plan()
        self.assertEqual(plan.status, 'READY', plan.payload_json)
        materialized = self.materialize(plan)
        record = json.loads(materialized.payload_json)['record_path']
        request = uuid4().hex
        before_count = len(self.store.snapshot()['jobs'])
        # Discard the committed response exactly as a disconnected transport would.
        self.service.register(self.parent['id'], record, plan.plan_digest, request, self.parent['attempt_id'])
        journal = self.store.request_lookup(request)
        child = journal['result']
        catalogue = CatalogueStore(self.store)
        query = CatalogueQuery(limit=1)
        cursor = catalogue.query(query).next_cursor
        catalogue.edit(child['id'], {'series': 'reviewed-successors'}, expected_revision=0,
                       expected_attempt_id=child['attempt_id'])
        relation = RelationStore(self.store).add(child['id'], parent=self.fixture['target'], kind='next-stage',
                    expected_revision=1, expected_attempt_id=child['attempt_id'])
        self.assertEqual(relation.revision, 2)
        with self.assertRaisesRegex(JobsError, 'cursor invalidated'):
            catalogue.query(CatalogueQuery(limit=1, cursor=cursor))
        before = self.store.path.read_bytes()
        inspect_local(self.store.root, child['id'], child['attempt_id'])
        replay = SuccessorService(self.store).register(self.parent['id'], record, plan.plan_digest,
                                                       request, self.parent['attempt_id'])
        returned = json.loads(replay.payload_json)
        self.assertTrue(returned['replayed'])
        self.assertEqual(returned['child']['attempt_id'], child['attempt_id'])
        self.assertEqual(returned['child']['status'], 'Hold')
        self.assertEqual(len(self.store.snapshot()['jobs']), before_count + 1)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(catalogue.read(child['id']).revision, 2)
        with self.assertRaises(JobsError):
            self.service.register(self.parent['id'], record, plan.plan_digest, uuid4().hex,
                                  self.parent['attempt_id'])
        self.assertFalse(self.store.snapshot()['controller']['online'])
        self.assertFalse((Path(self.spec['runtime']['run_directory']) / 'native').exists())
        trace('lost-registration', ['plan', 'materialize', 'register and discard reply', 'catalogue edit',
              'explicit relation', 'old cursor refused', 'doctor read', 'same request returns one original Hold child'])

    def test_changed_native_source_refuses_old_approval_without_publication(self):
        plan = self.plan()
        source = self.fixture['run'] / 'OUTCAR'
        original = source.read_bytes()
        before = self.store.path.read_bytes()
        try:
            source.write_bytes(original + b'\nsynthetic later native evidence\n')
            with self.assertRaisesRegex(JobsError, 'changed'):
                self.materialize(plan)
            self.assertFalse(Path(self.spec['adapter']['output']).exists())
            self.assertEqual(self.store.path.read_bytes(), before)
        finally:
            source.write_bytes(original)
        trace('source-mutation', ['plan', 'mutate native source', 'old approval refused', 'no publication or registration'])

    def test_closure_change_after_publication_retains_incomplete_record_without_registration(self):
        from cmw.jobs.successor import _closure
        plan = self.plan()
        before = self.store.path.read_bytes()
        output = Path(self.spec['adapter']['output'])
        record = Path(json.loads(plan.payload_json)['adapter']['publication_plan']['record_directory']) / 'successor.json'

        def changed_after_publication(*args):
            return {'closed': False, 'reason': 'injected closure drift'} if output.exists() else _closure(*args)

        with patch('cmw.jobs.successor._closure', side_effect=changed_after_publication):
            with self.assertRaisesRegex(JobsError, 'closure changed'):
                self.materialize(plan)
        self.assertEqual({p.name for p in output.iterdir()}, {'INCAR', 'POSCAR', 'KPOINTS', 'POTCAR'})
        self.assertEqual(json.loads(record.read_text())['status'], 'INCOMPLETE')
        with self.assertRaisesRegex(JobsError, 'invalid'):
            self.service.register(self.parent['id'], str(record), plan.plan_digest, uuid4().hex,
                                  self.parent['attempt_id'])
        self.assertEqual(self.store.path.read_bytes(), before)
        trace('publication-closure-drift', ['plan', 'publish four inputs', 'inject closure drift',
                                          'INCOMPLETE record retained', 'registration refused', 'queue unchanged'])

    def test_record_disk_full_after_publication_cannot_create_or_register_a_child(self):
        plan = self.plan()
        before = self.store.path.read_bytes()
        output = Path(self.spec['adapter']['output'])
        record = Path(json.loads(plan.payload_json)['adapter']['publication_plan']['record_directory']) / 'successor.json'
        link = os.link

        def disk_full_at_exclusive_create(source, destination):
            if Path(destination) == record:
                self.assertEqual(json.loads(Path(source).read_text())['status'], 'PENDING_VERIFICATION')
                raise OSError(errno.ENOSPC, 'injected record disk full', str(record))
            return link(source, destination)

        with patch('cmw.jobs.successor.os.link', side_effect=disk_full_at_exclusive_create):
            with self.assertRaises(OSError) as caught:
                self.materialize(plan)
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertEqual({p.name for p in output.iterdir()}, {'INCAR', 'POSCAR', 'KPOINTS', 'POTCAR'})
        self.assertFalse(record.exists())
        with self.assertRaises((JobsError, OSError)):
            self.service.register(self.parent['id'], str(record), plan.plan_digest, uuid4().hex,
                                  self.parent['attempt_id'])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual({p.name for p in record.parent.iterdir()}, {'preparation.json'})
        preparation = (record.parent / 'preparation.json').read_bytes()
        self.assertEqual(json.loads(preparation)['publication']['state'], 'complete')
        self.assertEqual(list(Path(self.spec['runtime']['run_directory']).iterdir()), [])
        published = {p.name: p.read_bytes() for p in output.iterdir()}

        # Recovery reuses the approved publication after space is available again.
        self.assertEqual(self.materialize(plan).status, 'MATERIALIZED')
        self.assertEqual(json.loads(record.read_text())['plan_digest'], plan.plan_digest)
        self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, published)
        self.assertEqual((record.parent / 'preparation.json').read_bytes(), preparation)
        self.assertEqual(self.store.path.read_bytes(), before)

        # Recover by planning fresh destinations, preserving the first publication as evidence.
        self.spec, self.path = successor_case(self.fixture, uuid4().hex)
        recovery = self.plan()
        self.assertNotEqual(recovery.plan_digest, plan.plan_digest)
        with self.assertRaisesRegex(JobsError, 'review the new plan digest'):
            self.materialize(plan)
        self.assertEqual(self.materialize(recovery).status, 'MATERIALIZED')
        self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, published)
        self.assertEqual((record.parent / 'preparation.json').read_bytes(), preparation)
        self.assertEqual(json.loads(record.read_text())['status'], 'MATERIALIZED')
        self.assertEqual(self.store.path.read_bytes(), before)
        trace('publication-disk-full', ['plan', 'publish four inputs', 'exclusive successor record create hits ENOSPC',
                                       'no authoritative record', 'registration refused', 'same destination recovered',
                                       'fresh destinations and reviewed digest materialized', 'queue unchanged'])
