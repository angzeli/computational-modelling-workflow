"""Prospective compact visual inventory and retained transition/failure rendering.

Adapted from the retained closure qlmanage renderer; no gate/app execution.

Every original SVG is passed unchanged to exactly:
/usr/bin/qlmanage -t -s2000 -o TASK_OWNED_DIRECTORY ORIGINAL.svg
No fallback, dependency installation, semantic oracle or visual verdict is used.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import html
import json
from pathlib import Path
import struct
import subprocess

from tests.campaign.visual_contract_v2 import plan, frame_id


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, indent=2)+'\n')


def inventory(roots):
    """Bind all 32 required IDs to actual exports; retain every extra capture."""
    roots = {name: Path(root).resolve() for name, root in roots.items()}
    contract = plan()
    required = {row['id']: row for row in contract['core']+contract['modals']}
    candidates = {key: set() for key in required}
    exports = {}
    def visit(value, directory, group, root):
        if isinstance(value, dict):
            directory = Path(value.get('directory', directory))
            identity = value.get('required_id')
            if identity is not None:
                assert identity in required, 'Unknown required visual ID: '+str(identity)
                assert value.get('size') == required[identity]['size'], (identity, value.get('size'))
                assert isinstance(value.get('image'), str), 'Required ID has no image: '+identity
                source = (directory/value['image']).resolve()
                assert source.is_relative_to(root), 'Image reference escaped its gate root'
                if source.is_file():
                    candidates[identity].add((group, str(source)))
            for child in value.values():
                visit(child, directory, group, root)
        elif isinstance(value, list):
            for child in value:
                visit(child, directory, group, root)
    for group, root in roots.items():
        root = Path(root).resolve()
        for path in root.rglob('result.json'):
            visit(json.loads(path.read_text()), path.parent, group, root)
        for source in sorted(root.rglob('*.svg')):
            if source.is_file():
                exports[str(source.resolve())] = group
                sidecar = source.with_suffix('.json')
                if sidecar.is_file():
                    visit(json.loads(sidecar.read_text()), source.parent, group, root)
    rows = []
    for identity, requirement in required.items():
        found = candidates[identity]
        assert len(found) <= 1, 'Duplicate distinct exports for required ID: '+identity
        group, source = next(iter(found)) if found else (
            'history' if requirement['family'] in {'static', 'relaxation'} else 'views', None)
        rows.append({**requirement, 'id': identity, 'group': group, 'source': source,
                     'required': True, 'phase': 'settled',
                     'checks': ['Exact immutable target, literal native values, card/controls fit; inspect actual image.']})
        exports.pop(source, None)
    for source, group in exports.items():
        name = Path(source).name
        phase = 'failure' if 'failure' in name else 'transition' if 'transition' in name else 'supplemental'
        rows.append({'id': group+':'+str(Path(source).relative_to(roots[group])),
                     'group': group, 'source': source, 'required': phase == 'failure', 'phase': phase,
                     'checks': ['Retain actual transition/failure contents; no replacement by a settled successor.']})
    for index, row in enumerate(rows):
        row['inspection_batch'] = index % 3 + 1
    return rows


def historical_mapping(previous):
    """Account for each old frame, including cases represented by trace assertions."""
    rows = []
    for old in previous['frames']:
        group, view, path = old['group'], old['view'], old['relative_path']
        frontend = 'local' if 'local' in path or view.startswith('local') else 'nodes'
        if group == 'active_writer':
            ids = []
            assertions = ['same 12 cycles, pages0/50/100 and first/last Details-return captures',
                          'actual writer heartbeat, immutable cursor/target, later normal refresh, queue unchanged']
        elif group == 'history':
            if view.endswith(('doctor', 'review')):
                family = 'doctor' if view.endswith('doctor') else 'review'
                ids = [frame_id(frontend, family, (80, 24))]
            else:
                ids = [frame_id(frontend, family, size) for family in ('static', 'relaxation')
                       for size in ((140, 40), (100, 30), (77, 24), (80, 24))]
            assertions = ['full five-size native resize sequence; real Details/Back and fresh input',
                          'literal native metrics, immutable target, geometry settlement and later refresh',
                          'two-owner failed-peer isolation, selected Review/Doctor and native search retained']
        elif group == 'views':
            assert view in {'catalogue','study','review','doctor','analysis','copy','review-copy'}, view
            if view in {'copy', 'review-copy'}:
                ids = []
                assertions = ['frozen body/attempt identity; native mouse Refresh; nested return; transition captures retained']
            elif view == 'analysis':
                ids = [frame_id(frontend, 'multiwfn', size) for size in ((140,40),(100,30),(77,24),(80,24))]
                assertions = ['all five resize transitions and mouse scroll captures',
                              'ANALYSIS/STAGE/OUTPUT plus native workflow literal fields and no invented SCF metrics']
            else:
                ids = [frame_id(frontend, view, (80,24))]
                assertions = ['all five resize/page-down/page-up/mouse-scroll traces and transition captures',
                              'modal/body/target/bounds assertions at every original checkpoint']
        else:
            raise AssertionError('Unmapped historical visual group: '+group)
        rows.append({'historical_id': old['id'], 'historical_phase': old['phase'],
                     'required_settled_ids': ids, 'retained_assertions': assertions,
                     'historical_disposition': 'UNCHANGED'})
    assert len({row['historical_id'] for row in rows}) == len(previous['frames']) == previous['total']
    return {'schema':2,'historical_total':previous['total'], 'prospective_required_settled':32,
            'historical_counts':dict(Counter(row['group']+'/'+row['phase'] for row in previous['frames'])),
            'mapped':len(rows),'unmapped':0,'rows':rows}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--historical-inventory', type=Path)
    parser.add_argument('--roots', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args=parser.parse_args()
    roots={name:Path(value).resolve() for name,value in json.loads(args.roots.read_text()).items()}
    assert set(roots)=={'history','views','active_writer'}, 'Exactly three current gate roots required'
    output=args.output.resolve();output.mkdir(mode=0o700)
    rows=inventory(roots)
    if args.historical_inventory:
        mapping=historical_mapping(json.loads(args.historical_inventory.read_text()))
        write(output/'historical-scenario-mapping.json',mapping)
    report={'schema':2,'contract':plan(),'roots':{k:str(v) for k,v in roots.items()},
        'frozen_rendering':['/usr/bin/qlmanage','-t','-s2000','-o','TASK_OWNED_DIRECTORY','ORIGINAL.svg'],
        'originals':'Passed unchanged; byte identity checked before and after each render',
        'fallback':'None. Any qlmanage failure remains an explicit blocker; alternatives need separate disposition.',
        'per_image_timeout_seconds':10,
        'visual_acceptance':'PENDING_INDEPENDENT_PER_IMAGE_INSPECTION','frames':rows}
    for index,row in enumerate(rows,1):
        row.update(visual_status='UNINSPECTED',inspector=None,findings=[])
        source=Path(row['source']) if row.get('source') else None
        if source is None or not source.is_file():
            row['render_status']='MISSING_REQUIRED_EXPORT'
            write(output/'manifest.json',report)
            continue
        frame_dir=output/(f'{index:03d}-'+row['group']+'-'+source.stem)
        frame_dir.mkdir(mode=0o700)
        original=source.read_bytes();row['source_sha256']=sha(original)
        command=['/usr/bin/qlmanage','-t','-s2000','-o',str(frame_dir),str(source)]
        row['render_command']=command
        try:
            completed=subprocess.run(command,capture_output=True,check=False,timeout=10)
            (frame_dir/'stdout.log').write_bytes(completed.stdout)
            (frame_dir/'stderr.log').write_bytes(completed.stderr)
            row['render_returncode']=completed.returncode
            images=sorted(frame_dir.glob('*.png'))
            assert completed.returncode==0, f'qlmanage returned {completed.returncode}'
            assert len(images)==1, f'Expected one native thumbnail, found {len(images)}'
            image=images[0];pixels=image.read_bytes()
            assert pixels[:8]==b'\x89PNG\r\n\x1a\n', 'Output is not PNG'
            row.update(png=str(image.relative_to(output)),png_sha256=sha(pixels),
                       rendered_size=list(struct.unpack('>II',pixels[16:24])),render_status='RENDERED')
        except subprocess.TimeoutExpired as exc:
            (frame_dir/'stdout.log').write_bytes(exc.stdout or b'')
            (frame_dir/'stderr.log').write_bytes(exc.stderr or b'')
            row.update(render_status='RENDER_FAILED',render_error=repr(exc),
                       render_timed_out=True,render_timeout_seconds=10)
        except Exception as exc:
            row.update(render_status='RENDER_FAILED',render_error=repr(exc))
        finally:
            row['original_bytes_unchanged']=source.read_bytes()==original
            if not row['original_bytes_unchanged']:
                row.update(render_status='SOURCE_BYTES_CHANGED',render_error='Original SVG bytes changed during rendering')
            write(output/'manifest.json',report)
    report['counts']={'declared':32,'discovered':len(rows),
        'rendered':sum(row['render_status']=='RENDERED' for row in rows),
        'missing':sum(row['render_status']=='MISSING_REQUIRED_EXPORT' for row in rows),
        'failed':sum(row['render_status'] not in {'RENDERED','MISSING_REQUIRED_EXPORT'} for row in rows)}
    report['render_complete']=report['counts']['missing']==report['counts']['failed']==0
    write(output/'manifest.json',report)
    for batch in (1,2,3):
        selected=[row for row in rows if row['inspection_batch']==batch]
        write(output/f'inspection-batch-{batch}.json',{'schema':1,'batch':batch,'frames':selected})
        sections=[]
        for row in selected:
            picture=('<a href="'+html.escape(row['png'],quote=True)+'"><img src="'+html.escape(row['png'],quote=True)+'"></a>') if row.get('png') else '<strong>Missing/failed image remains a blocker.</strong>'
            sections.append('<section><h2>'+html.escape(row['id'])+'</h2><p>Phase: '+row['phase']+' · '+row['render_status']+' · Visual: UNINSPECTED</p><code>'+html.escape(row.get('source') or 'Required ID has no exported image')+'</code><p>'+html.escape(' · '.join(row.get('checks',[])))+'</p>'+picture+'</section>')
        (output/f'inspection-batch-{batch}.html').write_text('<!doctype html><meta charset="utf-8"><title>Native evidence '+str(batch)+'</title><style>body{font:16px system-ui;background:#eee;color:#111}section{margin:28px 0}img{display:block;max-width:100%;border:1px solid #777}code{word-break:break-all}</style>'+''.join(sections))
    print(json.dumps(report['counts']))
    return 0 if report['render_complete'] else 1


if __name__=='__main__':
    raise SystemExit(main())
