import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";
import {runInNewContext} from "node:vm";

const code = readFileSync(new URL("../../static/submissions/workflow_alerts.js", import.meta.url), "utf8");
function reader(fetchStatus) {
    const module = {exports:{}};
    runInNewContext(code, {module, AbortController, setTimeout, clearTimeout, Date});
    const shown = [], failed = [];
    return {shown, failed, api:module.exports.createReader({fetchStatus,
        show:html=>shown.push(html), unavailable:date=>failed.push(date), timeout:30})};
}
const response = html => ({ok:true, redirected:false, text:async()=>html});
test("a newer refresh discards an old response even when transport ignores abort", async()=>{
    const pending=[];
    const p=reader(signal=>new Promise(resolve=>pending.push({signal,resolve})));
    const old=p.api.refresh();
    const current=p.api.refresh(true);
    assert.equal(pending[0].signal.aborted,true);
    pending[1].resolve(response("no warnings"));await current;
    pending[0].resolve(response("obsolete warnings"));await old;
    assert.deepEqual(p.shown,["no warnings"]);
    assert.equal(p.failed.length,0);
});
test("polls coalesce while reading; pause cancels results without reporting false zero",async()=>{
    let finish;
    const p=reader(()=>new Promise(resolve=>finish=resolve));
    const first=p.api.refresh();await p.api.refresh();
    p.api.pause();finish(response("old"));await first;
    assert.equal(p.shown.length,0);assert.equal(p.failed.length,0);
});
test("failure retains last successful status timestamp; recovery replaces it",async()=>{
    let ok=true;
    const p=reader(async()=>ok?response("known warning"):{ok:false});
    await p.api.refresh();ok=false;await p.api.refresh();
    assert.deepEqual(p.shown,["known warning"]);assert.ok(p.failed[0]);
    ok=true;await p.api.refresh();assert.equal(p.shown.length,2);
});
test("first failure or redirect is unknown, not a clear status",async()=>{
    const p=reader(async()=>({...response("login"),redirected:true}));
    await p.api.refresh();assert.deepEqual(p.failed,[null]);assert.equal(p.shown.length,0);
});
test("a stalled request times out and can be retried",async()=>{
    const p=reader(signal=>new Promise((resolve,reject)=>signal.addEventListener("abort",()=>reject(new Error("timeout")))));
    await p.api.refresh();assert.equal(p.failed.length,1);
    await p.api.refresh();assert.equal(p.failed.length,2);
});

function boot(fetcher = async()=>response("")) {
    class Node {
        constructor() {this.events={};this.dataset={};this.innerHTML="";this.textContent="";this.classList={add(){},remove(){}};}
        addEventListener(name,callback) {(this.events[name]||=[]).push(callback);}
        fire(name,event={}) {for(const callback of this.events[name]||[]) callback(event);}
        setAttribute() {} removeAttribute() {}
    }
    const doc=new Node(), win=new Node(), host=new Node(), nodes=new Map();
    for(const name of ["content","error","error-message","retry"]) nodes.set(`[data-workflow-alert-${name}]`,new Node());
    nodes.get("[data-workflow-alert-content]").innerHTML="Checking workflow status...";
    host.querySelector=key=>nodes.get(key);host.dataset.cfmWorkflowAlerts="/ui/workflow-alerts/";
    doc.querySelector=()=>host;doc.hidden=false;
    const timers=new Map(), intervals=new Map(), calls=[];let id=0;
    const storage=new Map();
    const localStorage={setItem:(key,value)=>storage.set(key,value)};
    const sessionStorage={...localStorage,getItem:key=>storage.get(key),removeItem:key=>storage.delete(key)};
    runInNewContext(code,{document:doc,window:win,localStorage,sessionStorage,AbortController,Date,
        setTimeout:(fn,ms)=>{timers.set(++id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id),
        setInterval:(fn,ms)=>{intervals.set(++id,{fn,ms});return id;},clearInterval:id=>intervals.delete(id),
        fetch:(url,options)=>{calls.push({url,options});return fetcher(url,options);}});
    return {doc,win,calls,timers,intervals,storage,nodes,
        async settle(){await new Promise(resolve=>setImmediate(resolve));},
        flush(){for(const [id,timer] of timers) if(timer.ms===80){timers.delete(id);timer.fn();}}};
}
test("visible-only polling and history/tab return refresh without mutating forms",async()=>{
    const p=boot();await p.settle();assert.equal(p.calls.length,1);
    assert.equal([...p.intervals.values()][0].ms,15000);
    p.doc.hidden=true;p.doc.fire("visibilitychange");assert.equal(p.intervals.size,0);
    p.doc.hidden=false;p.doc.fire("visibilitychange");await p.settle();assert.equal(p.calls.length,2);
    p.doc.fire("htmx:historyRestore");await p.settle();assert.equal(p.calls.length,3);
    p.win.fire("pageshow",{persisted:true});await p.settle();assert.equal(p.calls.length,4);
    p.win.fire("pagehide");assert.equal(p.intervals.size,0);assert.equal(p.timers.size,0);
    assert.ok(p.calls.every(call=>!call.options.method&&!call.options.body&&call.options.cache==="no-store"));
});
test("POST completion invalidates other tabs, GET does not; received notifications do not loop",async()=>{
    const p=boot();await p.settle();
    p.doc.fire("htmx:afterRequest",{detail:{requestConfig:{verb:"get"}}});p.flush();await p.settle();
    assert.equal(p.calls.length,1);
    p.doc.fire("htmx:afterRequest",{detail:{requestConfig:{verb:"post"}}});p.flush();await p.settle();
    assert.equal(p.calls.length,2);const notice=p.storage.get("cfm:workflow-changed");assert.ok(notice);
    p.win.fire("storage",{key:"cfm:workflow-changed"});p.flush();await p.settle();
    assert.equal(p.calls.length,3);assert.equal(p.storage.get("cfm:workflow-changed"),notice);
});
test("bursts coalesce and background tabs do not read until resumed",async()=>{
    const p=boot();await p.settle();
    p.doc.hidden=true;p.doc.fire("visibilitychange");
    for(let n=0;n<5;n++) p.win.fire("storage",{key:"cfm:workflow-changed"});
    p.flush();await p.settle();assert.equal(p.calls.length,1);
    p.doc.hidden=false;p.doc.fire("visibilitychange");await p.settle();assert.equal(p.calls.length,2);
    for(let n=0;n<5;n++) p.win.fire("focus");
    p.flush();await p.settle();assert.equal(p.calls.length,3);
});
test("initial read failure removes the loading placeholder and retry recovers",async()=>{
    let available=false;
    const p=boot(async()=>available?response("current warning"):{ok:false});await p.settle();
    assert.equal(p.nodes.get("[data-workflow-alert-content]").innerHTML,"");
    assert.match(p.nodes.get("[data-workflow-alert-error-message]").textContent,/unavailable/);
    available=true;p.nodes.get("[data-workflow-alert-retry]").fire("click");await p.settle();
    assert.equal(p.nodes.get("[data-workflow-alert-content]").innerHTML,"current warning");
});
