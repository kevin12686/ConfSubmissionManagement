"""Exact context anchors, bounded local alignment, explicit inspectable exceptions."""
from array import array
from collections import Counter,defaultdict
import statistics
from . import core, symbols
from . import layout as hybrid

MAX_CHARS=1600
SCALE=8192


def units(tokens):
    return [{'char':c,'token':t,'token_index':i,'char_index':j,'owner':t.get('owner')}
            for i,t in enumerate(tokens) for j,c in enumerate(t['text']) if not c.isspace()]


def visual_alignment(a,b,profile):
    """Direct pair rules only; never repeatedly canonicalize or use word bags."""
    if max(len(a),len(b))>MAX_CHARS:raise ValueError('Visual work bound exceeded')
    rules=symbols.rules(profile);ends=defaultdict(list)
    for ri,(left,right,rid) in enumerate(rules):ends[left[-1],right[-1]].append((ri,left,right))
    aa=''.join(u['char'] for u in a);bb=''.join(u['char'] for u in b)
    n,m=len(a),len(b);cost=[array('I',[0])*(m+1) for _ in range(n+1)]
    back=[array('H',[0])*(m+1) for _ in range(n+1)]
    for i in range(1,n+1):cost[i][0]=i*SCALE;back[i][0]=3
    for j in range(1,m+1):cost[0][j]=j*SCALE;back[0][j]=4
    for i in range(1,n+1):
        for j in range(1,m+1):
            same=a[i-1]['char']==b[j-1]['char'] and a[i-1]['owner']==b[j-1]['owner']
            best=(cost[i-1][j-1]+(0 if same else SCALE),1 if same else 2)
            for candidate in ((cost[i-1][j]+SCALE,3),(cost[i][j-1]+SCALE,4)):
                if candidate[0]<best[0]:best=candidate
            for ri,left,right in ends.get((aa[i-1],bb[j-1]),[]):
                p,q=i-len(left),j-len(right)
                if p<0 or q<0 or aa[p:i]!=left or bb[q:j]!=right:continue
                owners={u['owner'] for u in a[p:i]+b[q:j]}
                if len(owners)!=1:continue
                candidate=(cost[p][q]+1,ri+5)
                if candidate[0]<best[0]:best=candidate
            cost[i][j],back[i][j]=best
    path=[];i,j=n,m
    while i or j:
        op=back[i][j];rid=None
        if op>=5:
            left,right,rid=rules[op-5];p,q=i-len(left),j-len(right);kind='tolerated'
        else:p,q=i-(op!=4),j-(op!=3);kind={1:'equal',2:'change',3:'change',4:'change'}[op]
        path.append((kind,p,i,q,j,rid));i,j=p,q
    path.reverse();merged=[]
    for kind,p,i,q,j,rid in path:
        if merged and kind==merged[-1][0] and kind!='tolerated':
            k,x,_,y,_,_=merged[-1];merged[-1]=(k,x,i,y,j,None)
        else:merged.append((kind,p,i,q,j,rid))
    assert sum(i-p for _,p,i,_,_,_ in merged)==n
    assert sum(j-q for _,_,_,q,j,_ in merged)==m
    return merged


def text(part):
    out=[];last=None
    for u in part:
        if last is not None and last!=u['token_index']:out.append(' ')
        out.append(u['char']);last=u['token_index']
    return ''.join(out)


def item(a,b,reason,kind,rid=None):
    def tokens(part):
        unique={u['token_index']:u['token'] for u in part}
        return [unique[k] for k in sorted(unique)]
    left,right=tokens(a),tokens(b)
    return {'kind':kind,'reason':reason,'rule_id':rid,
            'before_text':text(a),'after_text':text(b),'before':core.locs(left),'after':core.locs(right),
            'before_tokens':len(left),'after_tokens':len(right),
            'before_characters':len(a),'after_characters':len(b)}


def compare_tokens(a,b,profile):
    anchors=core.anchors(a,b);deltas=defaultdict(list);votes=Counter()
    for i,j,k,l in anchors:
        for x,y in zip(range(i,j),range(k,l)):
            deltas[a[x]['page']].append((b[y]['x']-a[x]['x'],b[y]['y']-a[x]['y']))
            votes[a[x]['page'],b[y]['page']]+=1
    offsets={p:tuple(statistics.median(v[i] for v in ds) for i in (0,1)) for p,ds in deltas.items()}
    out={k:[] for k in ('findings','handoffs','info','ignored_spacing','ignored_visual')}
    ca=cb=equal_chars=0
    def add(left,right,reason,category,rid=None,context_left=None,context_right=None):
        f=item(left,right,reason,category,rid)
        for side, part in (('before', left), ('after', right)):
            records = {}
            for unit in part:
                token = unit['token']
                records[token['uid']] = {'page': token['page'], 'text': token.get('raw_text', token['text']), 'score': token['score'], 'box': token.get('raw_box', token['box'])}
            f[side + '_ocr'] = list(records.values())
        f['before_context']=' '.join(t['text'] for t in (context_left or []))
        f['after_context']=' '.join(t['text'] for t in (context_right or []))
        key={'author_review':'handoffs','ignored_visual':'ignored_visual','ignored_spacing':'ignored_spacing','wrap_hyphen':'info'}.get(category,'findings')
        out[key].append(f)
    for i,j,k,l in anchors+[(len(a),len(a),len(b),len(b))]:
        aa,bb=a[ca:i],b[cb:k];ua,ub=units(aa),units(bb)
        if aa or bb:
            contexta=a[max(0,ca-6):min(len(a),i+6)];contextb=b[max(0,cb-6):min(len(b),k+6)]
            if core.spacing_equivalent(aa,bb):
                add(ua,ub,'Identical ordered characters; whitespace split/join tolerated.','ignored_spacing',context_left=contexta,context_right=contextb)
            elif max(len(aa),len(bb))>core.CONFIG['maximum_gap_tokens']:
                add(ua,ub,'Span exceeds the bounded local comparison range.','author_review',context_left=contexta,context_right=contextb)
            else:
                strict=core.local_alignment(aa,bb,offsets);merged=[]
                for op,x,y,u,v in strict:
                    if merged and op!='equal' and merged[-1][0]!='equal':
                        _,p,_,q,_=merged[-1];merged[-1]=('replace',p,y,q,v)
                    else:merged.append((op,x,y,u,v))
                pending=[];reason=None;used_visual=False;residual_budgets=[]
                for op,x,y,u,v in merged:
                    left,right=aa[x:y],bb[u:v];lu,ru=units(left),units(right)
                    if op=='equal':pending.append(('equal',lu,ru,None));continue
                    if core.spacing_equivalent(left,right):pending.append(('spacing',lu,ru,None));continue
                    if core.wrap_hyphen(left,right):pending.append(('wrap',lu,ru,None));continue
                    if max(len(lu),len(ru))>MAX_CHARS:reason='Visual work bound exceeded';break
                    path=visual_alignment(lu,ru,profile)
                    # Exempt only a complete local difference explained by the
                    # table. A few coincidental similar characters inside a real
                    # edit must not fragment or partially excuse that edit.
                    if not any(kind=='tolerated' for kind,_,_,_,_,_ in path) or any(kind=='change' for kind,_,_,_,_,_ in path):
                        pending.append(('change',lu,ru,None))
                        residual_budgets.append((max(len(left),len(right)),max(len(lu),len(ru)),1));continue
                    used_visual=True
                    ordinary_left=[];ordinary_right=[]
                    for kind,p,e,q,f,rid in path:
                        if kind=='equal' and text(lu[p:e])!=text(ru[q:f]):kind='spacing'
                        pending.append((kind,lu[p:e],ru[q:f],rid))
                        if kind=='change':ordinary_left.extend(lu[p:e]);ordinary_right.extend(ru[q:f])
                    if ordinary_left or ordinary_right:
                        residual_budgets.append((max(len({v['token_index'] for v in ordinary_left}),len({v['token_index'] for v in ordinary_right})),max(len(ordinary_left),len(ordinary_right)),1))
                changed=[s for s in pending if s[0]=='change']
                if used_visual:
                    # The small-edit budget counts original token spans, not the
                    # number of character fragments exposed by a visual rule.
                    edits=sum(v[0] for v in residual_budgets)
                    chars=sum(v[1] for v in residual_budgets);run_count=sum(v[2] for v in residual_budgets)
                else:
                    strict_changes=[s for s in strict if s[0]!='equal']
                    edits=sum(max(y-x,v-u) for _,x,y,u,v in strict_changes)
                    chars=sum(max(sum(len(t['text']) for t in aa[x:y]),sum(len(t['text']) for t in bb[u:v])) for _,x,y,u,v in strict_changes)
                    run_count=len(strict_changes)
                if not anchors and changed:reason='No reliable unchanged text context; unable to locate a counterpart.'
                if edits>core.CONFIG['maximum_edit_tokens'] or chars>core.CONFIG['maximum_edit_characters'] or run_count>core.CONFIG['maximum_edit_runs']:
                    reason='Difference exceeds the declared small-edit budget; author confirmation required.'
                if reason:add(ua,ub,reason,'author_review',context_left=contexta,context_right=contextb)
                else:
                    for kind,left,right,rid in pending:
                        if kind=='equal':equal_chars+=len(left);continue
                        cat={'tolerated':'ignored_visual','spacing':'ignored_spacing','wrap':'wrap_hyphen'}.get(kind,'changed' if left and right else 'removed' if left else 'added')
                        why='Direct visual exception; not proof that document text is unchanged.' if kind=='tolerated' else 'Possible line-wrap hyphen.' if kind=='wrap' else 'Compared inside unchanged surrounding text.'
                        add(left,right,why,cat,rid,contexta,contextb)
        equal_chars+=sum(len(t['text']) for t in a[i:j]);ca,cb=j,l
    before=sum(len(t['text']) for t in a);after=sum(len(t['text']) for t in b)
    for side,total in [('before',before),('after',after)]:
        assert equal_chars+sum(f[side+'_characters'] for group in out.values() for f in group)==total
    status='unlocated' if out['handoffs'] and not anchors else 'partial' if out['handoffs'] else 'compared'
    return dict(out,status=status,profile=profile,
                counts={'changes':len(out['findings']),'author_review':len(out['handoffs']),'info':len(out['info']),
                        'ignored_spacing':len(out['ignored_spacing']),'ignored_visual':len(out['ignored_visual']),
                        'unknown_before':sum(f['before_tokens'] for f in out['handoffs']),
                        'unknown_after':sum(f['after_tokens'] for f in out['handoffs']),
                        'before_characters':before,'after_characters':after,'equal_characters':equal_chars},
                page_alignment=[{'before':p,'after':q,'supported_tokens':n} for (p,q),n in sorted(votes.items())])


def compare_pages(pa,pb,na=None,nb=None,profile='broad'):
    a,la,wa=core.flatten(pa);b,lb,wb=core.flatten(pb)
    fields=core.bind_fields(a,b,la,lb)
    a,sa=hybrid.guide(a,pa,na or []);b,sb=hybrid.guide(b,pb,nb or [])
    result=compare_tokens(a,b,profile)
    result.update(warnings=wa+wb,fields=fields,locator={'before':sa,'after':sb})
    if wa or wb:result['status']='partial' if a or b else 'no_ocr_text'
    return result
