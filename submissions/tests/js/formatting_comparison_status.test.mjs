import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";
import {runInNewContext} from "node:vm";

const script=readFileSync(new URL("../../static/submissions/formatting_comparison_status.js",import.meta.url),"utf8");
function setup(results) {
    const nodes=new Map();for(const key of ["spinner","retry","local","execution","open","warning","info","message"])
        nodes.set(`[data-upload-comparison-${key}]`,{hidden:false,dataset:{},textContent:""});
    let panel={dataset:{comparisonToken:"signed-upload",comparisonUrl:"/upload-comparison/",comparisonReturn:"/formatting/?mode=single",comparisonCsrf:"csrf",comparisonAutostart:"true"},querySelector:s=>nodes.get(s)};
    const events={},calls=[],timers=new Map();let n=0;
    const document={readyState:"complete",querySelectorAll:()=>[panel],addEventListener:(key,fn)=>events[key]=fn};
    const window={location:{origin:"http://localhost"},setTimeout:fn=>{timers.set(++n,fn);return n;},clearTimeout:id=>timers.delete(id),addEventListener:(key,fn)=>events[key]=fn};
    runInNewContext(script,{document,window,URL,URLSearchParams,AbortController,fetch:async(...args)=>{calls.push(args);return{json:async()=>results.shift()};}});
    return {get panel(){return panel;},nodes,calls,events,replace(){panel={...panel,dataset:{...panel.dataset}};},async settle(){await new Promise(resolve=>setImmediate(resolve));},click(kind){const button=nodes.get(`[data-upload-comparison-${kind}]`);button.closest=s=>s==="[data-upload-comparison]"?panel:null;
        events.click({target:{closest:s=>s===`[data-upload-comparison-${kind}]`?button:null}});}};
}
test("automatic comparison stops on a structured choice and only a distinct click sends its signed consent",async()=>{
    const p=setup([{state:"pending"},{state:"awaiting_choice",retryable:true,message:"Remote unreachable",local_confirmation:"signed-pair-consent"},
        {state:"complete",execution:{label:"Local · CPU"},url:"/compare/?comparison=bound",warning_count:2,text_available:true}]);
    await p.settle();assert.equal(p.calls.length,2);assert.equal(p.nodes.get("[data-upload-comparison-local]").hidden,false);
    p.events["htmx:load"]();await p.settle();assert.equal(p.calls.length,2);
    p.click("local");await p.settle();assert.equal(p.calls.length,3);
    assert.equal(p.calls[2][1].method,"POST");assert.match(String(p.calls[2][1].body),/local_confirmation=signed-pair-consent/);
    assert.equal(p.nodes.get("[data-upload-comparison-execution]").textContent,"Compared by Local · CPU");
    assert.equal(p.nodes.get("[data-upload-comparison-local]").dataset.confirmation,"");
});
test("ordinary retry never carries a prior local consent and remote-only choice offers no CPU action",async()=>{
    const p=setup([{state:"pending"},{state:"awaiting_choice",retryable:true,message:"Remote unavailable",local_confirmation:""},{state:"busy",retryable:true,message:"same job"}]);
    await p.settle();assert.equal(p.nodes.get("[data-upload-comparison-local]").hidden,true);
    p.click("retry");await p.settle();assert.doesNotMatch(String(p.calls[2][1].body),/local_confirmation/);
});

test("a replaced HTMX panel revalidates a completed result instead of displaying a stale revision",async()=>{
    const p=setup([{state:"complete",url:"/compare/?comparison=old",execution:{label:"Remote · GPU"},warning_count:2,text_available:true},
        {state:"unavailable",message:"This upload is no longer the current saved revision.",retryable:false}]);
    await p.settle();assert.equal(p.nodes.get("[data-upload-comparison-open]").hidden,false);
    p.events["htmx:load"]();await p.settle();assert.equal(p.calls.length,1);
    p.replace();p.events["htmx:load"]();
    assert.equal(p.nodes.get("[data-upload-comparison-open]").hidden,true);
    await p.settle();assert.equal(p.calls.length,2);
    assert.equal(p.calls[1][1].method,"GET");assert.equal(p.calls[1][1].cache,"no-store");
    for(const key of ["open","execution","warning","info","retry","local"])
        assert.equal(p.nodes.get(`[data-upload-comparison-${key}]`).hidden,true,key);
    assert.match(p.nodes.get("[data-upload-comparison-message]").textContent,/no longer the current saved revision/);
});

test("pageshow revalidates the same DOM without recomputing a valid complete comparison",async()=>{
    const complete={state:"complete",url:"/compare/?comparison=current",text_available:true};
    const p=setup([complete,complete]);await p.settle();
    p.events.pageshow();await p.settle();
    assert.equal(p.calls.length,2);assert.ok(p.calls.every(c=>c[1].method==="GET"));
    assert.equal(p.nodes.get("[data-upload-comparison-open]").hidden,false);
});

test("lifecycle events deduplicate the in-flight build even if the panel is replaced",async()=>{
    const p=setup([{state:"pending"},{state:"complete",url:"/compare/?comparison=current",text_available:true}]);
    p.events.pageshow();p.replace();p.events["htmx:load"]();await p.settle();
    assert.equal(p.calls.length,2);
    assert.deepEqual(p.calls.map(c=>c[1].method),["GET","POST"]);
    assert.equal(p.nodes.get("[data-upload-comparison-open]").hidden,false);
    p.events["htmx:load"]();await p.settle();assert.equal(p.calls.length,2);
});

test("revalidation failure hides a previous successful result and permits explicit retry",async()=>{
    const p=setup([{state:"complete",url:"/compare/?comparison=old",text_available:true},
        {state:"failed",message:"The comparison could not be loaded.",retryable:true},
        {state:"complete",url:"/compare/?comparison=current",text_available:true}]);
    await p.settle();p.replace();p.events["htmx:load"]();await p.settle();
    assert.equal(p.nodes.get("[data-upload-comparison-open]").hidden,true);
    assert.equal(p.nodes.get("[data-upload-comparison-retry]").hidden,false);
    p.click("retry");await p.settle();assert.equal(p.calls[2][1].method,"POST");
    assert.match(p.nodes.get("[data-upload-comparison-open]").href,/comparison=current/);
});
