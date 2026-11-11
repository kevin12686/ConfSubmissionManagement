import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";
import {runInNewContext} from "node:vm";

const script=readFileSync(new URL("../../static/submissions/formatting_comparison_status.js",import.meta.url),"utf8");
function setup(results) {
    const nodes=new Map();for(const key of ["spinner","retry","local","execution","open","warning","info","message"])
        nodes.set(`[data-upload-comparison-${key}]`,{hidden:false,dataset:{},textContent:""});
    const panel={dataset:{comparisonToken:"signed-upload",comparisonUrl:"/upload-comparison/",comparisonReturn:"/formatting/?mode=single",comparisonCsrf:"csrf",comparisonAutostart:"true"},querySelector:s=>nodes.get(s)};
    const events={},calls=[],timers=new Map();let n=0;
    const document={readyState:"complete",querySelectorAll:()=>[panel],addEventListener:(key,fn)=>events[key]=fn};
    const window={location:{origin:"http://localhost"},setTimeout:fn=>{timers.set(++n,fn);return n;},clearTimeout:id=>timers.delete(id),addEventListener:(key,fn)=>events[key]=fn};
    runInNewContext(script,{document,window,URL,URLSearchParams,AbortController,fetch:async(...args)=>{calls.push(args);return{json:async()=>results.shift()};}});
    return {panel,nodes,calls,events,async settle(){await new Promise(resolve=>setImmediate(resolve));},click(kind){const button=nodes.get(`[data-upload-comparison-${kind}]`);button.closest=s=>s==="[data-upload-comparison]"?panel:null;
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
