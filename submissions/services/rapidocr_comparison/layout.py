"""Native-supported physical row ordering; literal OCR remains authoritative."""
import statistics
from collections import Counter,defaultdict
from . import core
from . import locator as block_locator

CONFIG=dict(block_locator.CONFIG,row_center_tolerance=.35,
            minimum_ocr_native_height_ratio=.45,maximum_ocr_native_height_ratio=2.,
            row_order='physical_x_only_within_same_geometric_native_row_and_visible_row')


def row_bands(entries):
    """Cluster by median baseline, without transitive chaining across rows."""
    bands=[]
    for entry in sorted(entries,key=lambda v:(v[2],v[0])):
        if bands:
            band=bands[-1];cy=statistics.median(v[2] for v in band);h=statistics.median(v[3] for v in band)
            if abs(entry[2]-cy)<=CONFIG['row_center_tolerance']*min(h,entry[3]):
                band.append(entry);continue
        bands.append([entry])
    return bands


def native_rows(native_pages):
    """PDF line IDs can split one justified visual line into separate spans."""
    lines={};slots={}
    for p in native_pages:
        groups=defaultdict(list)
        for wi,w in enumerate(p['words']):
            _,y0,_,y1=w['box'];groups[w['block']].append((wi,w,(y0+y1)/2,y1-y0))
        for bi,entries in groups.items():
            for ri,band in enumerate(row_bands(entries)):
                key=p['page'],bi,ri
                lines[key]=[w for _,w,_,_ in sorted(band,key=lambda v:v[0])]
                for wi,_,_,_ in band:slots[p['page'],wi]=key
    return lines,slots


def correct_rows(tokens,native_pages):
    """Permute existing OCR occurrences, never their text, values or ownership.

    Each side is ordered independently from its own visible boxes. Native word
    spelling and the other PDF's desired sequence do not participate. A stale
    native line cannot merge distinct visible rows. Repeated boxes retain their
    own OCR token order, including punctuation inside the same recognized word.
    """
    native={p['page']:p['words'] for p in native_pages};lines,native_slots=native_rows(native_pages)
    candidates=defaultdict(list)
    for i,t in enumerate(tokens):
        if t.get('locator')!='native_geometry':continue
        slot=t['native_slot'];key=native_slots[t['page'],slot]
        line=lines[key]
        if len(line)<2:continue
        # Do not infer LTR reading direction for vertical/RTL/native-disordered
        # lines. Unsupported hints leave the original OCR order intact.
        xs=[(v['box'][0]+v['box'][2])/2 for v in line]
        if not all(a<=b for a,b in zip(xs,xs[1:])) or max(xs)==min(xs):continue
        x0,y0,x1,y1=t['box'];height=y1-y0
        native_height=statistics.median(v['box'][3]-v['box'][1] for v in line)
        if not CONFIG['minimum_ocr_native_height_ratio']*native_height<=height<=CONFIG['maximum_ocr_native_height_ratio']*native_height:continue
        candidates[key].append((i,t,(y0+y1)/2,height))
    result=list(tokens);log=[]
    for key,group in candidates.items():
        for band in row_bands(group):
            if len({t['uid'] for _,t,_,_ in band})<2:continue
            old=sorted(band,key=lambda v:v[0])
            new=sorted(band,key=lambda v:(v[1]['box'][0],v[0]))
            if [i for i,_,_,_ in old]==[i for i,_,_,_ in new]:continue
            for (slot,_,_,_),(_,token,_,_) in zip(old,new):result[slot]=token
            log.append({'page':key[0],'native_block':key[1],'geometric_native_row':key[2],
                        'original_native_line_ids':sorted({w['line'] for w in lines[key]}),
                        'before_order':' '.join(t['text'] for _,t,_,_ in old),
                        'after_order':' '.join(t['text'] for _,t,_,_ in new),
                        'locations':core.locs([t for _,t,_,_ in old])})
    signature=lambda seq:Counter((t['uid'],tuple(t['box']),t['text'],t.get('owner')) for t in seq)
    assert signature(result)==signature(tokens)
    return result,log


def guide(tokens,pages,native_pages):
    ordered,stats=block_locator.guide(tokens,pages,native_pages)
    ordered,log=correct_rows(ordered,native_pages)
    return ordered,dict(stats,row_reorders=len(log),reorder_log=log)
