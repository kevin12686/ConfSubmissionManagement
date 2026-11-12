"""Occurrence-preserving OCR anchors and bounded local edit alignment."""
import difflib
import math
import re
import statistics
import unicodedata
from collections import Counter,defaultdict

from . import tokens as helpers
from . import hyphens

CONFIG={'anchor_tokens':6,'anchor_characters':24,'maximum_gap_tokens':200,
        'maximum_edit_tokens':16,'maximum_edit_characters':80,'maximum_edit_runs':6,
        'maximum_document_tokens':30000,'insertion_cost':2,'deletion_cost':2,
        'substitution_cost':1,'case_sensitive':True,'spelling_correction':False,
        'ignore_split_join_whitespace':True}


def normalized(text):
    return ' '.join(unicodedata.normalize('NFC',text).split())


def flatten(pages):
    out=[];raw_lines={};warnings=[]
    for p in pages:
        if p['ocr']['status']!='completed':raise ValueError('OCR unavailable on page '+str(p['page']))
        if not p['ocr']['lines']:
            warnings.append({'page':p['page'],'reason':'No OCR text; blank versus unreadable is not certified.'})
        for i,line in enumerate(helpers.xy_order(p['ocr']['lines'],p['width'])):
            uid=(p['page'],i);raw_lines[uid]=dict(line,page=p['page'],w=p['width'],h=p['height'])
            lt=helpers.line_tokens(line,p['page'],i)
            for ti,t in enumerate(lt):
                t=dict(t,uid=uid,owner=None,at_line_start=ti==0,at_line_end=ti==len(lt)-1)
                t['x']=(t['box'][0]+t['box'][2])/2/p['width']
                t['y']=p['page']-1+(t['box'][1]+t['box'][3])/2/p['height']
                out.append(t)
    if len(out)>CONFIG['maximum_document_tokens']:raise ValueError('Document token work bound exceeded')
    return out,raw_lines,warnings


def field_candidates(lines):
    """Infer repeated label/value rows, not arbitrary nearby identical numbers."""
    result=[]
    steps=0
    for uid,right in lines.items():
        if not re.match(r'^[+\-\u2212]?\d',right['text'].lstrip()):continue
        rx0,ry0,rx1,ry1=right['box'];height=max(1,ry1-ry0)
        choices=[]
        for lid,left in lines.items():
            steps += 1
            if steps > 5_000_000:
                raise ValueError('OCR field ownership work bound exceeded')
            if left['page']!=right['page']:continue
            text=normalized(left['text']);words=helpers.lex(text)
            if not 1<=len(words)<=4 or not any(c.isalpha() for c in text):continue
            lx0,ly0,lx1,ly1=left['box']
            gap=rx0-lx1
            if gap<=0 or gap>right['w']*.4:continue
            if abs((ly0+ly1-ry0-ry1)/2)>max(height,ly1-ly0)*.6:continue
            choices.append((gap,lid,text,lx0/right['w']))
        if choices:
            gap,lid,label,lx=min(choices)
            result.append({'uid':uid,'label':label,'left':lid,'lx':lx,'rx':rx0/right['w'],'page':right['page']})
    return [r for r in result if sum(s['page']==r['page'] and abs(s['lx']-r['lx'])<.02
                                   and abs(s['rx']-r['rx'])<.02 for s in result)>=2]


def bind_fields(a,b,la,lb):
    aa,bb=field_candidates(la),field_candidates(lb)
    ca,cb=Counter(r['label'] for r in aa),Counter(r['label'] for r in bb)
    stable={s for s in ca if ca[s]==1 and cb[s]==1}
    for tokens,rows in ((a,aa),(b,bb)):
        owners={r['uid']:r['label'] for r in rows if r['label'] in stable}
        for t in tokens:t['owner']=owners.get(t['uid'])
    return sorted(stable)


def key(token):return token['text'],token.get('owner')


def spacing_equivalent(a,b):
    """User-selected tolerance: identical ordered characters, ignoring spacing.

    Ownership is retained per character so equal digits from different labelled
    fields cannot cancel each other. No case, symbol or spelling repair occurs.
    """
    if not a or not b:return False
    def chars(tokens):
        return [(char,t.get('owner')) for t in tokens for char in t['text'] if not char.isspace()]
    return chars(a)==chars(b)


def anchors(a,b,*,protect_hyphens=False):
    aa,bb=[key(t) for t in a],[key(t) for t in b];n=CONFIG['anchor_tokens']
    if aa and aa == bb:
        return [(0,len(a),0,len(b))]
    frequency_a,frequency_b=Counter(aa),Counter(bb)
    if sum(count*frequency_b[key] for key,count in frequency_a.items()) > 12_000_000:
        raise ValueError('OCR anchor pairing work bound exceeded')
    ca=Counter(tuple(aa[i:i+n]) for i in range(max(0,len(aa)-n+1)))
    cb=Counter(tuple(bb[i:i+n]) for i in range(max(0,len(bb)-n+1)))
    inside_a,inside_b=(hyphens.interiors(a),hyphens.interiors(b)) if protect_hyphens else ({},{})
    accepted=[]
    for i,j,size in difflib.SequenceMatcher(None,aa,bb,autojunk=False).get_matching_blocks():
        end_i,end_j=i+size,j+size
        while i<end_i and (i in inside_a or j in inside_b):
            step=max(inside_a.get(i,(i,i))[1]-i,inside_b.get(j,(j,j))[1]-j)
            i+=step;j+=step
        while end_i>i and (end_i in inside_a or end_j in inside_b):
            step=max(end_i-inside_a.get(end_i,(end_i,end_i))[0],end_j-inside_b.get(end_j,(end_j,end_j))[0])
            end_i-=step;end_j-=step
        size=end_i-i
        if size<n or sum(len(t['text']) for t in a[i:i+size])<CONFIG['anchor_characters']:continue
        if any(ca[tuple(aa[p:p+n])]==1 and cb[tuple(aa[p:p+n])]==1 for p in range(i,i+size-n+1)):
            accepted.append((i,i+size,j,j+size))
    if not accepted and aa and aa==bb:
        accepted=[(0,len(a),0,len(b))]
    return accepted


def local_alignment(a,b,offsets,*,hyphen_pairs=None):
    """Bounded exact edit DP; substitutions beat delete/insert number swapping.

    The second score is geometry only as a tie breaker. It never makes differing
    tokens equal and cannot beat a lower integer edit cost.
    """
    n,m=len(a),len(b)
    pairs=hyphen_pairs or {}
    costs=[[(0,0.0)]*(m+1) for _ in range(n+1)];back=[[None]*(m+1) for _ in range(n+1)]
    for i in range(1,n+1):costs[i][0]=(i*CONFIG['deletion_cost'],0);back[i][0]='delete'
    for j in range(1,m+1):costs[0][j]=(j*CONFIG['insertion_cost'],0);back[0][j]='insert'
    for i in range(1,n+1):
        for j in range(1,m+1):
            same=key(a[i-1])==key(b[j-1]);c=costs[i-1][j-1]
            geo=0.0
            if same:
                dx,dy=offsets.get(a[i-1]['page'],(0,0))
                geo=abs(a[i-1]['x']+dx-b[j-1]['x'])+abs(a[i-1]['y']+dy-b[j-1]['y'])
            choices=[((c[0]+(0 if same else CONFIG['substitution_cost']),c[1]+geo),'equal' if same else 'replace'),
                     ((costs[i-1][j][0]+CONFIG['deletion_cost'],costs[i-1][j][1]),'delete'),
                     ((costs[i][j-1][0]+CONFIG['insertion_cost'],costs[i][j-1][1]),'insert')]
            value,op=min(choices,key=lambda item:item[0]);costs[i][j]=value;back[i][j]=op
            for start,left in pairs.get((i,j),[]):
                candidate=(costs[start][left][0]+1,costs[start][left][1])
                if candidate<costs[i][j]:
                    costs[i][j]=candidate;back[i][j]=('word_hyphen',start,left)
    path=[];i,j=n,m
    while i or j:
        op=back[i][j]
        if isinstance(op,tuple):op,ai,bj=op
        else:ai=i-(op!='insert');bj=j-(op!='delete')
        path.append((op,ai,i,bj,j));i,j=ai,bj
    path.reverse();runs=[]
    for op,i,j,k,l in path:
        if runs and runs[-1][0]==op and op!='word_hyphen':
            old=runs[-1];runs[-1]=(op,old[1],j,old[3],l)
        else:runs.append((op,i,j,k,l))
    return runs


def locs(tokens):
    return [{'page':p,'box':list(box),'precision':precision} for p,box,precision in
            sorted({(t['page'],tuple(t['box']),t['precision']) for t in tokens})]


def wrap_hyphen(a,b):
    """Label an already validated lexical pair; this is not a tolerance gate."""
    def wrapped(left,right):
        return (len(left)==3 and len(right)==1 and left[1]['text']=='-'
                and left[0]['text'].isalpha() and left[2]['text'].isalpha()
                and min(len(left[0]['text']),len(left[2]['text']))>=2
                and left[0].get('uid')!=left[2].get('uid')
                and left[1].get('at_line_end') and left[2].get('at_line_start')
                and left[0]['text']+left[2]['text']==right[0]['text'])
    return wrapped(a,b) or wrapped(b,a)
