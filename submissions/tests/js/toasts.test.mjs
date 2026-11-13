import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";
import {runInNewContext} from "node:vm";
const code=readFileSync(new URL("../../static/submissions/toasts.js",import.meta.url),"utf8");
function setup() {
    class Node {
        constructor(){this.dataset={};this.textContent="";this.hidden=false;this.events={};this.attributes={};this.children=[];
            this.classList={remove:name=>this.removedClass=name};this.style={};}
        addEventListener(name,fn){(this.events[name]||=[]).push(fn);}
        fire(name,event={}){for(const fn of this.events[name]||[])fn(event);}
        setAttribute(k,v){this.attributes[k]=v;}
        appendChild(n){n.parent=this;this.children.push(n);}
        remove(){this.parent.children=this.parent.children.filter(n=>n!==this);}
    }
    function toast(){const n=new Node(),parts=new Map();for(const key of ['close','title','message','record'])parts.set(`[data-cfm-toast-${key}]`,new Node());n.querySelector=k=>parts.get(k);return n;}
    const container=new Node(),template={content:{firstElementChild:{cloneNode:toast}}},doc=new Node(),win=new Node();
    container.querySelectorAll=()=>container.children;
    let headerBottom=118,id=0;
    const targets=new Map();
    const timers=new Map(),frames=new Map();
    doc.querySelector=s=>s==='[data-cfm-toast-container]'?container:{getBoundingClientRect:()=>({bottom:headerBottom})};
    doc.getElementById=id=>id==='cfm-toast-template'?template:targets.get(id);doc.querySelectorAll=()=>[];
    win.setTimeout=(fn,ms)=>{timers.set(++id,{fn,ms});return id;};
    win.requestAnimationFrame=fn=>{frames.set(++id,fn);return id;};win.cancelAnimationFrame=id=>frames.delete(id);
    runInNewContext(code,{document:doc,window:win});
    const feedback=(level,message,record="")=>Object.assign(new Node(),{dataset:{cfmFeedbackLevel:level,cfmFeedbackRecord:record},textContent:message});
    const swap=nodes=>doc.fire('htmx:afterSwap',{detail:{target:{querySelectorAll:()=>nodes}}});
    return {container,doc,win,timers,frames,feedback,swap,targets,setHeader:value=>headerBottom=value};
}
test("partial save uses the same Toast, including identity, with literal text",()=>{
    const p=setup(),note=p.feedback('success','<script>literal, not HTML</script>','Paper P035 / Final 35');p.swap([note]);
    const n=p.container.children[0];assert.equal(n.className,'toast show cfm-toast cfm-toast-success');
    assert.equal(n.querySelector('[data-cfm-toast-message]').textContent,'<script>literal, not HTML</script>');
    assert.equal(n.querySelector('[data-cfm-toast-record]').textContent,'Paper P035 / Final 35');
    assert.equal(note.hidden,true);assert.equal(n.attributes.role,'status');
    assert.equal([...p.timers.values()][0].ms,5000);
});
test("a restored/redispatched fragment does not replay an already shown success",()=>{
    const p=setup(),note=p.feedback('success','Saved');p.swap([note]);p.swap([note]);
    assert.equal(p.container.children.length,1);
});
test("outerHTML replacement reads the live target rather than the detached original",()=>{
    const p=setup(),note=p.feedback('success','Saved live row');
    p.targets.set('worklist',{querySelectorAll:()=>[note],isConnected:true});
    p.doc.fire('htmx:afterSwap',{detail:{target:{id:'worklist',isConnected:false,querySelectorAll:()=>[]}}});
    assert.equal(p.container.children.length,1);assert.equal(note.dataset.cfmFeedbackPresented,'true');
});
test("validation retains inline evidence and an error Toast never auto-hides",()=>{
    const p=setup(),note=p.feedback('danger','Reason required');p.swap([note]);
    const n=p.container.children[0];assert.equal(note.hidden,false);assert.equal(n.dataset.bsAutohide,'false');
    assert.equal(n.attributes.role,'alert');assert.equal(p.timers.size,0);
});
test("position follows visible header, falls back to top margin, close works",()=>{
    const p=setup();p.win.CFMToast.show({level:'warning',message:'Attention'});
    assert.equal(p.container.style.top,'130px');p.setHeader(-100);p.win.fire('scroll');
    for(const fn of p.frames.values())fn();assert.equal(p.container.style.top,'12px');
    const n=p.container.children[0];n.querySelector('[data-cfm-toast-close]').fire('click');
    assert.equal(n.removedClass,'show');[...p.timers.values()].find(t=>t.ms===150).fn();assert.equal(p.container.children.length,0);
});
