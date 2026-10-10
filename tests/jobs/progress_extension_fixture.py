"""Bounded invented dialect for shared-contract acceptance; not CP2K output."""
from collections import OrderedDict
from dataclasses import replace

from cmw.progress.models import (Criterion, HistoryColumn, HistoryEntry, IterationLevel,
                                 Metric, MetricSeries, ProgressSnapshot, Timing, comparable)
from cmw.progress.registry import SourcePlan, register_adapter


ENGINE = 'FICTIONAL-EXTENSION'
TEXT = ('OPT,1,1,1,1,0.004,2.5\n'
        'OPT,1,1,1,2,0.003,3.5\n'
        'SP,2,1,1,1,0.002,?\n')


class ExtensionAdapter:
    """geometry -> outer -> inner, with explicitly non-interchangeable metrics."""
    def __init__(self, context):
        self.context = context
        self.entries = OrderedDict()
        self.pending = OrderedDict()
        self.sequence = 0
        self.latest = ()
        self.previous = ()
        self.columns = (HistoryColumn('native_label', 'NATIVE CONTEXT'),
                        HistoryColumn('component', 'MAX COMPONENT [Ha/bohr]', 'fixture.maximum_component', 'metric'),
                        HistoryColumn('norm', 'ATOMIC NORM [Ha/bohr]', 'fixture.atomic_norm', 'metric'),
                        HistoryColumn('flux', 'CHARGE FLUX [C/s]', 'fixture.flux', 'metric'),
                        HistoryColumn('duration', 'DURATION / SOURCE', kind='timing'))

    def update(self, updates, now):
        for update in updates:
            if update.reset:
                self.entries.clear()
                self.pending.clear()
                self.latest = self.previous = ()
                self.sequence = 0
            for line in update.lines:
                stage, segment, geometry, outer, inner, token, duration = line.text.split(',')
                segment = int(segment)
                indices = tuple(map(int, (geometry, outer, inner)))
                levels = tuple(IterationLevel(kind, index, label) for kind, index, label in
                    zip(('geometry', 'outer', 'inner'), indices, ('geometry', 'outer solver', 'inner solver')))
                value = float(token)
                common = dict(context=levels, stage=stage, segment=segment, evidence=(line.evidence,),
                              observed_at=now, comparison_context=levels[:-1])
                metrics = (
                    Metric('fixture.maximum_component', 'Maximum component',
                           'Largest absolute Cartesian gradient component; not a vector norm', 'Ha/bohr', value,
                           criterion=Criterion(.001, 'Ha/bohr', '<', 'invented native threshold'), priority=1, **common),
                    Metric('fixture.atomic_norm', 'Atomic vector norm',
                           'Largest per-atom Euclidean gradient-vector norm', 'Ha/bohr', value * 1.5,
                           priority=2, **common),
                    Metric('fixture.flux', 'Charge flux', 'Invented charge per simulated second', 'C/s', value * 100,
                           priority=3, **common))
                self.previous, self.latest = self.latest, metrics
                parent = None
                for depth in range(1, 4):
                    key = f'{update.state.generation}:{stage}:{segment}:' + '/'.join(map(str, indices[:depth]))
                    old = self.entries.get(key)
                    if old is None:
                        self.sequence += 1
                        old = HistoryEntry(key, self.sequence, levels[:depth], parent, kind=levels[depth-1].kind,
                            native_label=levels[depth-1].label+' '+str(indices[depth-1]),
                            stage=stage, segment=segment, binding_id=self.context.binding_id,
                            attempt_id=self.context.attempt_id, state='COMPLETE', coverage='COMPLETE')
                    timing = () if depth == 2 else (Timing('duration', 'Explicit fixture duration',
                        None if duration == '?' else float(duration), context=levels[:depth], stage=stage,
                        segment=segment, source='unavailable' if duration == '?' else 'native',
                        completeness='UNAVAILABLE' if duration == '?' else 'COMPLETE',
                        evidence=(line.evidence,), reason='Not printed' if duration == '?' else ''),)
                    children = {entry.entry_id for entry in self.entries.values() if entry.parent_id == key}
                    if depth < 3:
                        children.add(f'{update.state.generation}:{stage}:{segment}:' + '/'.join(map(str, indices[:depth+1])))
                    entry = replace(old, metrics=metrics, timings=timing, evidence=(line.evidence,), child_count=len(children))
                    self.entries[key] = self.pending[key] = entry
                    parent = key
                    while len(self.entries) > 192:
                        self.entries.popitem(last=False)
                    while len(self.pending) > 192:
                        self.pending.popitem(last=False)
        pairs = tuple(MetricSeries(metric, previous if comparable(metric, previous) else None)
                      for metric, previous in zip(self.latest, self.previous)) if self.previous else tuple(MetricSeries(metric) for metric in self.latest)
        return ProgressSnapshot(self.context, 'invented-extension/v1', status='OBSERVING',
            phase=(self.latest[0].stage+' / inner solver') if self.latest else 'Pending',
            iteration=self.latest[0].context if self.latest else (), metrics=pairs,
            history_columns=self.columns, completeness='COMPLETE' if self.latest else 'PENDING', read_health='OK')

    def drain_history(self):
        entries = tuple(sorted(self.pending.values(), key=lambda entry: entry.sequence))
        self.pending.clear()
        return entries


def register():
    register_adapter(ENGINE, ExtensionAdapter, sources=(SourcePlan('synthetic', 'progress.synthetic'),))
