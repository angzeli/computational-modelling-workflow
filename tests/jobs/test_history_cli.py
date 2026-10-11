"""Read-only history commands and an engine-independent, three-level fixture."""
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cmw.cli import main
from cmw.jobs.store import Store
from cmw.progress.models import (Criterion, HistoryColumn, HistoryEntry, IterationLevel,
                                 Metric, MetricSeries, ProgressSnapshot, Timing)
from cmw.progress.registry import register_adapter


class FictionalHistoryAdapter:
    """Fictional charge flux with geometry → outer solver → inner solver context."""
    def __init__(self, context):
        self.context = context
        self.entries = {}
        self.pending = {}
        self.sequence = 0
        self.latest = self.previous = None
        self.columns = (HistoryColumn('native_label', 'NATIVE CONTEXT'),
                        HistoryColumn('child_count', 'CHILDREN', kind='children'),
                        HistoryColumn('flux', 'CHARGE FLUX [C/s]', 'fictional.flux', 'metric'),
                        HistoryColumn('duration', 'DURATION / SOURCE', kind='timing'),
                        HistoryColumn('state', 'STATE'))

    def update(self, updates, now):
        for update in updates:
            if update.reset:
                self.entries.clear()
                self.pending.clear()
                self.sequence = 0
                self.latest = self.previous = None
            for line in update.lines:
                geometry, outer, inner, value = line.text.split(',')
                indices = (int(geometry), int(outer), int(inner))
                levels = tuple(IterationLevel(kind, index, label) for kind, index, label in
                    zip(('geometry', 'outer', 'inner'), indices, ('geometry', 'outer solver', 'inner solver')))
                metric = Metric('fictional.flux', 'Charge flux', 'Fictional charge per simulated second', 'C/s', float(value),
                    levels, stage='fixture-stage', evidence=(line.evidence,), observed_at=now,
                    criterion=Criterion(.1, 'C/s', '<', 'fictional native control'), comparison_context=levels[:-1])
                self.previous = self.latest if self.latest and self.latest.context[:-1] == levels[:-1] else None
                self.latest = metric
                parent = None
                for depth in range(1,4):
                    key = f'g{update.state.generation}:' + '/'.join(str(i) for i in indices[:depth])
                    entry = self.entries.get(key)
                    if entry is None:
                        self.sequence += 1
                        entry = HistoryEntry(key, self.sequence, levels[:depth], parent, kind=levels[depth-1].kind,
                            native_label=levels[depth-1].label+' '+str(indices[depth-1]), state='COMPLETE', coverage='COMPLETE',
                            binding_id=self.context.binding_id, attempt_id=self.context.attempt_id, stage='fixture-stage')
                    if depth == 1:
                        timing = Timing('duration', 'Observed presence', lower_bound=1.25, source='observer-derived',
                            quantity='observation-interval', clock='monotonic', context=levels[:depth], completeness='LOWER_BOUND',
                            reason='Observed only after an evaluation was already active; no full duration.')
                    elif depth == 2:
                        timing = Timing('duration', 'Outer duration', context=levels[:depth], reason='No recoverable native timing')
                    else:
                        timing = Timing('duration', 'Native inner duration', .5*indices[2], source='native', context=levels[:depth],
                            completeness='COMPLETE', evidence=(line.evidence,))
                    children = set(e.entry_id for e in self.entries.values() if e.parent_id == key)
                    if depth < 3:
                        children.add(f'g{update.state.generation}:' + '/'.join(str(i) for i in indices[:depth+1]))
                    entry = replace(entry, child_count=len(children), metrics=(metric,), timings=(timing,), evidence=(line.evidence,))
                    self.entries[key] = self.pending[key] = entry
                    parent = key
        return ProgressSnapshot(self.context, 'fictional-history/v1', status='OBSERVING', phase='Inner solver',
            iteration=self.latest.context if self.latest else (), metrics=(MetricSeries(self.latest,self.previous),) if self.latest else (),
            history_columns=self.columns, completed_durations=(Timing('duration','Completed fixture',2.5,source='native',completeness='COMPLETE'),
                                                               Timing('duration','Completed fixture',3.5,source='native',completeness='COMPLETE')),
            completeness='COMPLETE', read_health='OK')

    def drain_history(self):
        pending = tuple(sorted(self.pending.values(), key=lambda entry:entry.sequence))
        self.pending.clear()
        return pending


class HistoryCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cmw-history-cli-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root/'state')
        self.job = self.store.add(argv=['/unexecuted/fixture'],cwd=self.root,name='Synthetic history',engine='FICTIONAL-HISTORY',hold=True)
        self.source = self.root/'flux.txt'
        self.source.write_text('4,2,1,3.75\n4,2,2,3.25\n5,3,1,2.0\n')
        from cmw.progress.registry import _REGISTRY
        registration=patch.dict(_REGISTRY)
        registration.start()
        self.addCleanup(registration.stop)
        register_adapter('FICTIONAL-HISTORY', FictionalHistoryAdapter)

    def invoke(self,*args):
        out,err=io.StringIO(),io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code=main(['jobs','--state',str(self.store.root),'progress',self.job['display_id'],
                       '--source',f'flux={self.source}',*args])
        return code,out.getvalue(),err.getvalue()

    def test_native_history_pagination_and_three_nested_levels_through_normal_cli(self):
        before=self.store.path.read_bytes()
        code,out,err=self.invoke('--history','--limit','1','--json')
        self.assertEqual((code,err),(0,''))
        first=json.loads(out)
        self.assertEqual((first['status'],first['coverage'],first['total']),('READY','COMPLETE',2))
        self.assertIsNotNone(first['next_cursor'])
        code,out,_=self.invoke('--history','--limit','1','--cursor',first['next_cursor'],'--json')
        self.assertEqual(json.loads(out)['entries'][0]['native_label'],'geometry 5')
        parent=first['entries'][0]['entry_id']
        code,out,_=self.invoke('--history','--parent',parent,'--json')
        outer=json.loads(out)['entries'][0]
        self.assertEqual(len(outer['context']),2)
        code,out,_=self.invoke('--history','--parent',outer['entry_id'],'--json')
        child=json.loads(out)['entries'][0]
        self.assertEqual(len(child['context']),3)
        self.assertEqual(child['metrics'][0]['unit'],'C/s')
        self.assertEqual(child['timings'][0]['source'],'native')
        code,out,_=self.invoke('--history')
        self.assertIn('CHARGE FLUX [C/s]',out)
        self.assertIn('observer-derived',out)
        self.assertEqual(self.store.path.read_bytes(),before)
        self.assertEqual(self.store.snapshot()['jobs'][0]['status'],'Hold')

    def test_filters_empty_engine_and_options_stay_explicit(self):
        for option,value in (('--limit','0'),('--limit','201'),('--history-polls','0'),('--history-polls','4097'),('--segment','0')):
            code,out,_=self.invoke('--history',option,value,'--json')
            self.assertEqual((code,json.loads(out)['code']),(2,'JOBS_ERROR'))
        code,out,_=self.invoke('--parent','invented','--json')
        self.assertEqual(code,2)
        code,out,_=self.invoke('--history','--engine','NO_ADAPTER','--json')
        self.assertEqual((code,json.loads(out)['status']),(0,'ADAPTER_UNAVAILABLE'))
        code,out,_=self.invoke('--history','--stage','other-stage','--json')
        self.assertEqual(json.loads(out)['entries'],[])
        code,out,_=self.invoke('--history','--cursor','not-a-cursor','--json')
        self.assertEqual(json.loads(out)['status'],'CURSOR_INVALID')

    def test_bounded_history_reports_loading_without_claiming_full_history(self):
        from cmw.progress.reader import RegularFileReader
        from cmw.progress.service import ObservationService
        self.source.write_text(''.join(f'{i},1,1,2.5\n' for i in range(1,90)))
        observer=ObservationService(reader_factory=lambda source: RegularFileReader(source,byte_budget=32))
        with patch('cmw.progress.service.ObservationService',return_value=observer):
            code,out,_=self.invoke('--history','--history-polls','1','--json')
        result=json.loads(out)
        self.assertEqual((code,result['status'],result['coverage']),(0,'LOADING','PARTIAL'))
        self.assertIsNone(result['total'])

    def test_native_vasp_observation_interval_is_visible_without_claiming_duration(self):
        from cmw.progress.formatting import history_cell
        from cmw.progress.models import RunContext, SourceDescriptor
        from cmw.progress.service import ObservationService
        from tests.progress.test_vasp_history import timing_texts
        texts=timing_texts(evaluations=1,loops=False,outer=False)
        output=self.root/'OUTCAR'
        output.write_text(texts['OUTCAR'][:texts['OUTCAR'].index(' EDDAV:')])
        context=RunContext('vasp-observation-format','VASP',(SourceDescriptor('outcar',str(output)),))
        clock=[1]
        observer=ObservationService(monotonic_clock=lambda:clock[0])
        self.addCleanup(observer.close)
        observer.poll(context)
        clock[0]=5
        observer.history_page(context)
        clock[0]=8
        page=observer.history_page(context)
        column=next(column for column in page.columns if column.kind=='timing')
        value=history_cell(next(entry for entry in page.entries if entry.kind=='evaluation'),column)
        self.assertIn('~00:00:03',value)
        self.assertIn('observer',value)
        self.assertIn('observation-interval',value)

    def test_generic_timing_format_never_substitutes_cpu_or_cumulative_values(self):
        from cmw.progress.formatting import history_entry_text,timing_text,progress_text
        from cmw.progress.models import RunContext
        cpu=Timing('cpu','Native CPU',7,clock='cpu',source='native',completeness='COMPLETE')
        total=replace(cpu,timing_id='total',clock='wall',quantity='cumulative')
        unavailable=Timing('duration','Duration',reason='Missing native duration')
        entry=HistoryEntry('entry',1,timings=(cpu,total,unavailable))
        text=history_entry_text(entry)
        self.assertIn('CPU, native',text)
        self.assertIn('cumulative',text)
        self.assertIn('unavailable',text)
        snapshot=ProgressSnapshot(RunContext('test','FICTIONAL'),'fixture',completed_durations=(replace(cpu,clock='wall',context=(IterationLevel('evaluation',2,'evaluation'),)),
                                                                                    replace(cpu,clock='wall',context=(IterationLevel('evaluation',1,'evaluation'),))))
        self.assertIn('~00:00:07 wall/native @2 ← ~00:00:07 wall/native @1',progress_text(snapshot,compact=True,width=80))
        from cmw.progress.formatting import history_cell
        observed=Timing('different-id','Observed presence',1.25,clock='wall',source='observer',
                        quantity='observation-interval',completeness='PARTIAL')
        value=history_cell(replace(entry,timings=(observed,)),HistoryColumn('duration','Duration',kind='timing'))
        self.assertIn('observer',value)
        self.assertIn('observation-interval',value)
        value=history_cell(replace(entry,timings=(total,)),HistoryColumn('duration','Duration',kind='timing'))
        self.assertIn('cumulative',value)


if __name__=='__main__':
    unittest.main()
