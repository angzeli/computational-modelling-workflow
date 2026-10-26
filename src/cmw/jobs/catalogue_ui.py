"""Bounded, explicit catalogue views; query records never authorize job control."""
from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
import threading

from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from .catalogue_models import CatalogueQuery, CataloguePage, CatalogueRecord
from .copy_ui import SelectableStatic
from .snapshot import safe_snapshot_text


class CatalogueSearch(ModalScreen):
    BINDINGS = [Binding('escape', 'dismiss(None)', 'Back', priority=True),
                Binding('ctrl+enter', 'apply_query', 'Apply', priority=True)]
    DEFAULT_CSS = """
    CatalogueSearch { align: center middle; background: $background 70%; }
    CatalogueSearch > Vertical { width: 96%; max-width: 110; height: auto; max-height: 96%; border: round $accent; padding: 0 1; background: $surface; }
    CatalogueSearch Input { height: 1; border: none; margin: 0; padding: 0 1; }
    CatalogueSearch Input:focus { border: none; }
    CatalogueSearch Horizontal { height: 1; }
    CatalogueSearch Horizontal Input { width: 1fr; }
    CatalogueSearch #catalogue-coverage { height: 3; }
    CatalogueSearch #catalogue-summary { height: auto; }
    CatalogueSearch #catalogue-page-row { height: 3; }
    CatalogueSearch Select { width: 1fr; }
    CatalogueSearch #catalogue-actions { height: 3; }
    CatalogueSearch Button { width: 1fr; min-width: 10; }
    CatalogueSearch #catalogue-help { height: auto; max-height: 2; }
    """

    def __init__(self, query, pages, summary):
        super().__init__()
        self.criteria = query or CatalogueQuery()
        self.pages = pages
        self.summary = summary

    def compose(self):
        with Vertical():
            yield Label('CATALOGUE SEARCH · literal text / explicit filters')
            yield Input(self.criteria.text, placeholder='Text in names, aliases and notes', id='catalogue-text')
            for left, right in (('project','system'), ('series','research_stage'), ('engine','status'), ('node_id','queue_id')):
                with Horizontal():
                    for field in (left,right):
                        yield Input(getattr(self.criteria,field) or '', placeholder=field.replace('_',' ').title(), id='catalogue-'+field)
            yield Input(', '.join(self.criteria.tags), placeholder='Tags: comma separated, all required', id='catalogue-tags')
            with VerticalScroll(id='catalogue-coverage'):
                yield Static(self.summary, id='catalogue-summary', markup=False)
            with Horizontal(id='catalogue-page-row'):
                options = [(owner,owner) for owner,page in self.pages.items() if page is not None and page.next_cursor]
                yield Select(options, prompt='Owner with another page', id='catalogue-owner')
                yield Button('Next page', id='catalogue-next', disabled=not options)
            with Horizontal(id='catalogue-actions'):
                yield Button('Apply', id='catalogue-apply', variant='primary')
                yield Button('Clear', id='catalogue-clear')
                yield Button('Back', id='catalogue-back')
            yield Static('Apply: metadata only; queue order and admission unchanged.\nOwner pages are independent observations; Esc returns.', id='catalogue-help', markup=False)

    def on_mount(self):
        self.query_one('#catalogue-text', Input).focus()

    def action_apply_query(self):
        values = {field:self.query_one('#catalogue-'+field,Input).value.strip() or None
                  for field in ('project','system','series','research_stage','engine','status','node_id','queue_id')}
        try:
            query = CatalogueQuery(text=self.query_one('#catalogue-text',Input).value,
                tags=tuple(item.strip() for item in self.query_one('#catalogue-tags',Input).value.split(',') if item.strip()),
                limit=50, **values)
        except (ValueError,TypeError) as exc:
            self.query_one('#catalogue-summary',Static).update(safe_snapshot_text(str(exc)))
            return
        self.dismiss(('apply',query,None))

    def on_button_pressed(self,event):
        if event.button.id == 'catalogue-apply':
            self.action_apply_query()
        elif event.button.id == 'catalogue-clear':
            self.dismiss(('clear',None,None))
        elif event.button.id == 'catalogue-next':
            owner = self.query_one('#catalogue-owner',Select).value
            if owner is not Select.BLANK:
                self.dismiss(('next',self.criteria,owner))
        else:
            self.dismiss(None)


def catalogue_text(record, relations=None):
    lines = ['HUMAN ORGANIZATION · does not establish scientific equivalence',
             f'{record.node_id or "local"} / {record.queue_id or "unassigned"} / {record.display_id}',
             'Attempt: '+record.attempt_id, 'Submitted name: '+record.name]
    for field in ('alias','project','system','series','research_stage'):
        lines.append(field.replace('_',' ').title()+': '+(getattr(record,field) or 'unassigned'))
    lines.extend(('Tags: '+(', '.join(record.tags) or 'unassigned'), 'Note: '+(record.note or '—'),
                  'Metadata revision: '+str(record.revision)))
    if relations is not None:
        lines.append('\nRELATIONSHIPS · '+str(relations.coverage))
        for relation in relations.relations:
            parent = relation.parent
            lines.extend((f'{relation.kind} · {relation.basis} · {relation.state}',
                f'Parent: {parent["node_id"]} / {parent["queue_id"]} / J{parent["job_id"]}',
                'Attempt: '+parent['attempt_id'], 'Resolution: '+relation.resolution, relation.qualification))
            if relation.evidence:
                lines.extend(('Parent artifact: '+relation.evidence['parent_artifact_id'],
                              'Child artifact: '+relation.evidence['child_artifact_id']))
            lines.append('')
        if not relations.relations:
            lines.append('No declared relationships.')
    return safe_snapshot_text('\n'.join(lines))


class CatalogueDetails(ModalScreen):
    BINDINGS = [Binding('escape','dismiss(None)','Back',priority=True), Binding('q','dismiss(None)','Back'),
                Binding('m','series_members','Series members')]
    DEFAULT_CSS = """
    CatalogueDetails { align: center middle; background: $background 70%; }
    CatalogueDetails > Vertical { width: 96%; max-width: 138; height: 94%; border: round $accent; padding: 0 1; background: $surface; }
    CatalogueDetails #catalogue-details-scroll { height: 1fr; }
    CatalogueDetails #catalogue-body { height: auto; }
    CatalogueDetails #catalogue-detail-actions { height: 3; }
    CatalogueDetails Button { width: 1fr; }
    CatalogueDetails #catalogue-detail-status { height: auto; max-height: 2; }
    """

    def __init__(self,label,request):
        super().__init__()
        self.label,self.request,self.record,self.relations = label,request,None,None

    def compose(self):
        with Vertical():
            yield Label(safe_snapshot_text('CATALOGUE · '+self.label))
            with VerticalScroll(id='catalogue-details-scroll'):
                yield SelectableStatic('Loading owner metadata…',id='catalogue-body',markup=False)
            yield Static('Intent and recorded artifact links never satisfy a scientific policy.',id='catalogue-detail-status',markup=False)
            with Horizontal(id='catalogue-detail-actions'):
                yield Button('M Series members',id='catalogue-members',disabled=True)
                yield Button('Back',id='catalogue-detail-back')

    def on_mount(self):
        def accepted(record,relations,error):
            if not self.is_mounted:
                return
            if error is not None:
                self.query_one('#catalogue-detail-status',Static).update(safe_snapshot_text('Catalogue unavailable: '+str(error)))
                return
            self.record,self.relations = record,relations
            self.query_one('#catalogue-body',Static).update(catalogue_text(record,relations))
            self.query_one('#catalogue-members',Button).disabled = not bool(record.series)
        self.request(accepted)
        self.query_one('#catalogue-details-scroll').focus()

    def action_series_members(self):
        if self.record and self.record.series:
            self.dismiss(('apply',CatalogueQuery(series=self.record.series,limit=50),None))

    def on_button_pressed(self,event):
        if event.button.id == 'catalogue-members':
            self.action_series_members()
        else:
            self.dismiss(None)


class CatalogueCommon:
    def init_catalogue(self):
        self.catalogue_query = None
        self.catalogue_pages = {}
        self.catalogue_errors = {}
        self.catalogue_generation = 0
        self.catalogue_auto_select = True
        self.catalogue_cancel = threading.Event()
        self.catalogue_busy = False
        self.catalogue_pending = None

    def catalogue_summary(self):
        if self.catalogue_query is None:
            return 'No catalogue filter. All queue commitments remain in the queue summary.'
        lines = []
        for owner in self.catalogue_owners():
            page = self.catalogue_pages.get(owner)
            error = self.catalogue_errors.get(owner) or (self.lanes[owner].error if hasattr(self,'lanes') else None)
            if error:
                lines.append(owner+': unavailable / partial coverage · '+error)
            elif page is None:
                lines.append(owner+': search pending; no visible control target')
            else:
                lines.append(f'{owner}: {page.matching_count} matching / {page.total_count} total · {len(page.records)} on page · / Search'
                    + (' · more' if page.next_cursor else '') + ' · '+str(page.coverage))
        if len(self.catalogue_owners()) > 1:
            pages = tuple(self.catalogue_pages.values())
            counts = {}
            for page in pages:
                for status,count in page.status_counts:
                    counts[status] = counts.get(status,0)+count
            missing = len(self.catalogue_owners())-len(pages)
            lines.insert(0,f'Observed: {sum(page.matching_count for page in pages)} matching / {sum(page.total_count for page in pages)} total · '
                f'Run {counts.get("Run",0)} Unknown {counts.get("Unknown",0)} · '
                + (f'{missing} owner(s) pending/unavailable; partial coverage' if missing else 'independent owner pages'))
        return safe_snapshot_text('\n'.join(lines))

    def action_search(self):
        self.push_screen(CatalogueSearch(self.catalogue_query,self.catalogue_pages,self.catalogue_summary()),self.catalogue_changed)

    def catalogue_changed(self,result):
        if result is None:
            return
        action,query,owner = result
        self.catalogue_auto_select = False
        self.catalogue_generation += 1
        self.catalogue_cancel.set()
        self.catalogue_cancel = threading.Event()
        self.catalogue_errors = {}
        if action == 'clear':
            self.catalogue_query = None
            self.catalogue_pages = {}
            self.catalogue_pending = None
            self.catalogue_refresh()
            return
        cursor = self.catalogue_pages[owner].next_cursor if action == 'next' else None
        self.catalogue_query = replace(query,cursor=None)
        if action != 'next':
            self.catalogue_pages = {}
        elif owner:
            self.catalogue_pages.pop(owner,None)
        self.catalogue_request(owner,cursor)
        self.catalogue_refresh()


class LocalCatalogue(CatalogueCommon):
    def catalogue_owners(self):
        return ('LOCAL',)

    def catalogue_refresh(self):
        self.refresh_state()

    def catalogue_request(self,owner=None,cursor=None):
        request = (self.catalogue_generation,replace(self.catalogue_query,cursor=cursor),self.catalogue_cancel)
        if self.catalogue_busy:
            self.catalogue_pending = request
            return
        self.catalogue_busy = True
        def collect():
            from .catalogue import CatalogueStore
            generation,query,cancel = request
            try:
                page,error = CatalogueStore(self.store).query(query,cancelled=cancel),None
            except Exception as exc:
                page,error = None,exc
            def accepted():
                self.catalogue_busy = False
                if generation == self.catalogue_generation and not cancel.is_set():
                    if error is not None:
                        self.catalogue_errors['LOCAL'] = str(error)
                    else:
                        self.catalogue_pages['LOCAL'] = page
                    self.catalogue_refresh()
                pending,self.catalogue_pending = self.catalogue_pending,None
                if pending and self.catalogue_query is not None:
                    self.catalogue_request(cursor=pending[1].cursor)
            try:
                self.call_from_thread(accepted)
            except RuntimeError:
                pass
        self.run_worker(collect,thread=True,group='catalogue-query',exit_on_error=False)

    def catalogue_jobs(self,jobs):
        if self.catalogue_query is None:
            return jobs
        page = self.catalogue_pages.get('LOCAL')
        index = {(job['id'],job['attempt_id']):job for job in jobs}
        visible = [index[(row.job_id,row.attempt_id)] for row in page.records
                   if (row.job_id,row.attempt_id) in index] if page else []
        if self.selected_id not in {job['display_id'] for job in visible}:
            self.selected_id = self._selected_label = self._selected_target = None
            self.suspension_selection_generation += 1
        return visible

    def action_catalogue(self):
        job = self.selected()
        if job is None or self.external_selected_id:
            return
        def request(callback):
            def collect():
                from .catalogue import CatalogueStore
                from .catalogue_relations import RelationStore
                try:
                    record = CatalogueStore(self.store).read(job['id'],expected_attempt_id=job['attempt_id'])
                    relations = RelationStore(self.store).list(job['id'],expected_attempt_id=job['attempt_id'])
                    error = None
                except Exception as exc:
                    record,relations,error = None,None,exc
                try:
                    self.call_from_thread(callback,record,relations,error)
                except RuntimeError:
                    pass
            self.run_worker(collect,thread=True,group='catalogue-details',exit_on_error=False)
        self.push_screen(CatalogueDetails(job['display_id'],request),self.catalogue_changed)


class NodeCatalogue(CatalogueCommon):
    def catalogue_owners(self):
        return tuple(self.lanes)

    def catalogue_refresh(self):
        self.render_state()

    def catalogue_request(self,owner=None,cursor=None):
        from cmw.nodes.codec import encode_typed,decode_typed
        for label in ([owner] if owner else self.lanes):
            lane = self.lanes[label]
            generation,session = self.catalogue_generation,lane.session
            lane.pending = type(lane.pending)(request for request in lane.pending if request[0] != 'catalogue.query')
            query = replace(self.catalogue_query,cursor=cursor if label == owner else None)
            def accepted(envelope,error,label=label,generation=generation,session=session):
                if generation != self.catalogue_generation or self.catalogue_query is None:
                    return
                if session is not None and self.lanes[label].session != session:
                    error = ValueError('Endpoint session changed; apply the search again')
                if error is not None:
                    self.catalogue_errors[label] = str(error)
                    self.catalogue_pages.pop(label,None)
                else:
                    try:
                        page = decode_typed(envelope['result']['page'],CataloguePage)
                        current = self.lanes[label].backend
                        if (page.node_id,page.queue_id) != (current.node_id,current.queue_id):
                            raise ValueError('Catalogue page belongs to another owner')
                        self.catalogue_pages[label] = page
                    except (ValueError,TypeError,KeyError) as exc:
                        self.catalogue_errors[label] = str(exc)
                self.render_state()
            if not self.queue(label,'catalogue.query',{'query':encode_typed(query)},callback=accepted):
                self.catalogue_errors[label] = 'Owner observation queue full or closed'

    def catalogue_rows(self,rows):
        if self.catalogue_query is None:
            return rows
        from cmw.nodes.portal import row_key,target_key
        visible = {}
        for owner,page in self.catalogue_pages.items():
            lane = self.lanes[owner]
            if lane.error or owner in self.catalogue_errors:
                continue
            for record in page.records:
                target = dict(node_id=record.node_id,queue_id=record.queue_id,job_id=record.job_id,attempt_id=record.attempt_id)
                key = row_key(target)
                if key in rows:
                    visible[key] = rows[key]
                else:
                    saved = lane.cache.get(target_key(target),{}).get('show')
                    if (saved and saved[0]['session_id'] == lane.session
                            and saved[0]['result']['job'].get('target') == target):
                        visible[key] = (owner,saved[0]['result']['job'])
                    else:
                        visible[key] = (owner,dict(id=record.job_id,attempt_id=record.attempt_id,display_id=record.display_id,
                            name=record.alias or record.name,status=record.status,engine=record.engine,target=target,
                            elapsed=None,_catalogue_pending=True))
        if self.selected_key not in visible:
            self.select(None)
        return visible

    def catalogue_hydrate(self,key):
        item = self.rows.get(key)
        if not item or not item[1].get('_catalogue_pending'):
            return
        owner,job = item
        generation,selection = self.catalogue_generation,self.selection_generation
        def accepted(envelope,error):
            if generation != self.catalogue_generation or key != self.selected_key or selection != self.selection_generation:
                return
            if error is not None:
                self.message('Selected metadata is not a control target: '+str(error))
                return
            from cmw.nodes.portal import row_key
            current = envelope['result']['job']
            if row_key(current['target']) != key:
                self.message('Owner returned a different attempt; selection remains unavailable.')
                return
            self.render_state()
        self.queue(owner,'show',target=job['target'],callback=accepted)

    def action_catalogue(self):
        from cmw.nodes.codec import decode_typed
        from .catalogue_relations import RelationPage
        selected = self.selected()
        if selected is None:
            return
        owner,job = selected
        target = deepcopy(job['target'])
        def request(callback):
            def record_accepted(envelope,error):
                if error is not None:
                    callback(None,None,error)
                    return
                try:
                    record = decode_typed(envelope['result']['record'],CatalogueRecord)
                    if record.target != target:
                        raise ValueError('Catalogue metadata belongs to a different attempt')
                except (ValueError,TypeError,KeyError) as exc:
                    callback(None,None,exc)
                    return
                def relations_accepted(envelope,error):
                    try:
                        relations = decode_typed(envelope['result']['relations'],RelationPage) if error is None else None
                        if relations is not None and any(getattr(relations,key) != value for key,value in target.items()):
                            raise ValueError('Relationships belong to a different attempt')
                    except (ValueError,TypeError,KeyError) as exc:
                        relations,error = None,exc
                    callback(record,relations,error)
                self.queue(owner,'catalogue.relations',target=target,callback=relations_accepted)
            self.queue(owner,'catalogue.show',target=target,callback=record_accepted)
        self.push_screen(CatalogueDetails(self.label(owner,job),request),self.catalogue_changed)
