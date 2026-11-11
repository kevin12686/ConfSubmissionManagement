import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";
import {runInNewContext} from "node:vm";

const code = readFileSync(new URL("../../static/submissions/comparison_services.js", import.meta.url), "utf8");
class Node {
    constructor() {this.dataset = {}; this.hidden = false; this.textContent = ""; this.listeners = {}; this.attributes = {};}
    addEventListener(name, fn) {(this.listeners[name] ||= []).push(fn);}
    fire(name) {for (const fn of this.listeners[name] || []) fn({currentTarget:this});}
    setAttribute(name, value) {this.attributes[name] = value;}
}
const observation = () => ({checked_at:"2026-11-11T18:00:00Z", remote:{state:"ready",backend:"cuda"},local:{state:"ready",backend:"cpu"}});
function setup(fetcher = async () => ({ok:true,json:async()=>observation()})) {
    const controls = new Map();
    for (const key of ["refresh", "checked", "details-toggle", "details", "remote-message", "local-message"]) controls.set(`[data-services-${key}]`, new Node());
    controls.get("[data-services-details]").hidden=true;
    const states = [];
    for (const name of ["remote","local"]) {
        const row = new Node(), backend = new Node(), state = new Node();
        row.querySelector = selector => selector === "[data-service-backend]" ? backend : state;
        controls.set(`[data-service="${name}"]`,row); controls.set(name+"-backend", backend); controls.set(name+"-state",state); states.push(state);
    }
    const root = new Node(); root.isConnected = true; root.dataset.servicesUrl="/reviews/formatting/comparison-services/";
    root.querySelector = key => controls.get(key); root.querySelectorAll = selector => selector === "[data-service-backend]" ? [controls.get("remote-backend"), controls.get("local-backend")] : states;
    const doc = new Node(); doc.hidden=false; doc.querySelector = () => root;
    const timers = new Map(); let id=0; const calls=[];
    const win = new Node(); win.setTimeout=(fn,ms)=>{timers.set(++id,{fn,ms});return id;}; win.clearTimeout=n=>timers.delete(n);
    runInNewContext(code,{document:doc,window:win,AbortController,Date,fetch:(...args)=>{calls.push(args);return fetcher(...args);}});
    return {root,doc,win,controls,timers,calls, async settle(){await new Promise(resolve=>setImmediate(resolve));},
        poll(){const item=[...timers].find(([,t])=>t.ms===10000);assert.ok(item);timers.delete(item[0]);item[1].fn();}};
}
test("one shared GET observation displays actual backends and refreshes every ten seconds",async()=>{
    const result=observation();
    const p=setup(async()=>({ok:true,json:async()=>result}));await p.settle();
    assert.equal(p.controls.get("remote-backend").textContent,"GPU");
    assert.equal(p.controls.get("local-backend").textContent,"CPU");
    assert.equal(p.calls.length,1);p.poll();await p.settle();assert.equal(p.calls.length,2);
    result.local={state:"embedded",backend:"cpu",backend_configured:true};
    p.poll();await p.settle();assert.equal(p.controls.get("local-backend").textContent,"CPU · configured");
    assert.equal(p.controls.get("local-state").textContent,"Embedded mode");
    assert.ok(p.calls.every(([url,options])=>url.endsWith("comparison-services/")&&!options.body&&!options.method));
});
test("no overlapping requests, including manual refresh and pageshow while checking",async()=>{
    let finish;const p=setup(()=>new Promise(resolve=>finish=resolve));
    p.controls.get("[data-services-refresh]").fire("click");p.win.fire("pageshow");assert.equal(p.calls.length,1);
    finish({ok:true,json:async()=>observation()});await p.settle();assert.equal(p.controls.get("[data-services-refresh]").disabled,false);
});
test("hidden tabs pause and abort in-flight observation; showing resumes without starting jobs",async()=>{
    const p=setup(async(_,options)=>new Promise((resolve,reject)=>options.signal.addEventListener("abort",()=>reject(new Error("aborted")))));
    p.doc.hidden=true;p.doc.fire("visibilitychange");await p.settle();
    assert.equal(p.calls[0][1].signal.aborted,true);assert.equal(p.timers.size,0);
    p.doc.hidden=false;p.doc.fire("visibilitychange");assert.equal(p.calls.length,2);
    p.win.fire("pagehide");await p.settle();assert.equal(p.timers.size,0);
});
test("unknown or failed checks never keep a green Ready label; last-seen GPU is explicit",async()=>{
    const result=observation();result.remote={state:"unreachable",backend:"",last_backend:"cuda",message:"Timed out"};
    let available=true;
    const p=setup(async()=>({ok:available,json:async()=>result}));await p.settle();
    assert.equal(p.controls.get("remote-backend").textContent,"GPU · last seen");assert.equal(p.controls.get("remote-state").textContent,"Unreachable");
    p.controls.get("[data-services-details-toggle]").fire("click");assert.equal(p.controls.get("[data-services-details-toggle]").attributes["aria-expanded"],"true");
    available=false;p.poll();await p.settle();
    assert.equal(p.controls.get("local-state").textContent,"Status unavailable");
    assert.match(p.controls.get("[data-services-local-message]").textContent,/could not be retrieved/);
    assert.equal(p.controls.get("local-backend").textContent,"CPU · last seen");
    const failed=setup(async()=>({ok:false}));await failed.settle();assert.equal(failed.controls.get("remote-state").textContent,"Status unavailable");
});
