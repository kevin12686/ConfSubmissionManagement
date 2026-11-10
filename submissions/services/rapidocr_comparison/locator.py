"""Native geometry hints order full-page OCR; only OCR text determines changes."""
import difflib
import statistics
import unicodedata
from collections import Counter,defaultdict
from . import core

CONFIG={'native_min_overlap':.35,'native_grid_pixels':128,'locator_granularity':'native_block',
        'locator_anchor_words':4,
        'locator_anchor_characters':16,'ocr_authority':True,'native_equality_suppression':False}


def area(box):return max(0,box[2]-box[0])*max(0,box[3]-box[1])


def intersection(a,b):return max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))


def guide(tokens,pages,native_pages):
    """Each OCR token appears once. Geometry, not native spelling, assigns slots.

    Native blocks/cells provide the outer order; original OCR order is retained
    inside each region so stale per-word geometry cannot undo natural reflow.
    Unattached tokens use a coarse nearest-block slot plus original OCR order.
    This is an explicit location hint, never a claim of native correspondence.
    No native words are inserted into the OCR stream.
    """
    native={p['page']:p['words'] for p in native_pages};indexed=[];stats=Counter()
    grouped=defaultdict(list)
    for i,t in enumerate(tokens):grouped[t['page']].append((i,t))
    for page in pages:
        pn=page['page'];words=native.get(pn,[]);page_tokens=grouped[pn]
        if not words:
            stats['ocr_only_pages']+=1
            for i,t in page_tokens:indexed.append(((pn,i,0),dict(t,locator='ocr_only')))
            stats['ocr_only_tokens']+=len(page_tokens);continue
        grid=defaultdict(set);blocks={};centres={}
        g=CONFIG['native_grid_pixels']
        for wi,w in enumerate(words):
            x0,y0,x1,y1=w['box']
            for x in range(int(x0//g),int(x1//g)+1):
                for y in range(int(y0//g),int(y1//g)+1):grid[x,y].add(wi)
            bi=w['block'];old=blocks.get(bi,w['box'])
            blocks[bi]=[min(old[0],x0),min(old[1],y0),max(old[2],x1),max(old[3],y1)]
            centres[wi]=((x0+x1)/2,(y0+y1)/2)
        for i,t in page_tokens:
            x0,y0,x1,y1=t['box'];cx,cy=(x0+x1)/2,(y0+y1)/2;candidate=set()
            for x in range(int(x0//g),int(x1//g)+1):
                for y in range(int(y0//g),int(y1//g)+1):candidate.update(grid[x,y])
            ranked=[]
            for wi in candidate:
                overlap=intersection(t['box'],words[wi]['box'])/max(1,area(t['box']))
                distance=((cx-centres[wi][0])/page['width'])**2+((cy-centres[wi][1])/page['height'])**2
                if overlap>=CONFIG['native_min_overlap']:ranked.append((-overlap,distance,wi))
            if ranked:
                wi=min(ranked)[2];kind='native_geometry';stats['native_geometry_tokens']+=1
            else:
                # Keep image text/new words even when no native word overlaps.
                # A nearby block is only an ordering hint for the OCR fallback.
                def box_distance(box):
                    return max(box[0]-cx,0,cx-box[2])**2+max(box[1]-cy,0,cy-box[3])**2
                bi=min(blocks,key=lambda bi:(box_distance(blocks[bi]),bi))
                options=[wi for wi,w in enumerate(words) if w['block']==bi]
                wi=min(options,key=lambda wi:(box_distance(words[wi]['box']),wi))
                kind='coarse_neighbor';stats['coarse_neighbor_tokens']+=1
            indexed.append(((pn,words[wi]['block'],i),dict(t,locator=kind,native_slot=wi,native_block=words[wi]['block'])))
    ordered=[t for _,t in sorted(indexed,key=lambda v:v[0])]
    assert len(ordered)==len(tokens)
    # A token's own literal recognition is immutable under native guidance.
    assert Counter((t['uid'],tuple(t['box']),t['text']) for t in ordered)==Counter((t['uid'],tuple(t['box']),t['text']) for t in tokens)
    return ordered,dict(stats)
