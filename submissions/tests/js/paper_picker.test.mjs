import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";
import {runInNewContext} from "node:vm";

const code=readFileSync(new URL("../../static/submissions/paper_picker.js",import.meta.url),"utf8");
function setup(initial="P006",valueField="paper_id") {
    class Node {
        constructor(){this.classList={add(){},remove(){}};this.children=[];this.value="";this.hidden=true;this.attributes={};}
        setAttribute(k,v){this.attributes[k]=v;} removeAttribute(k){delete this.attributes[k];}
        replaceChildren(...nodes){this.children=nodes;}
        append(...nodes){this.children.push(...nodes);}
        set innerHTML(text){this.markup=text;this.firstElementChild=new Node();}
        get innerHTML(){return String(this.textContent||"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");}
    }
    const element=new Node(),summary=new Node(),events={},requests=[],instances=[];
    element.value=initial;element.dataset={pickerValueField:valueField,pickerSummaryTarget:"summary",pickerUrl:"/ui/paper-picker/"};
    class Picker {
        constructor(input,settings){this.input=input;this.settings=settings;this.options={};this.items=[];this.loadedSearches={};this.wrapper=new Node();this.events={};input.tomselect=this;instances.push(this);}
        on(name,fn){this.events[name]=fn;}
        addOption(option){this.options[String(option[valueField])]=option;}
        removeOption(value){delete this.options[value];}
        getValue(){return this.items[0]||"";}
        setValue(value,silent){this.items=[String(value)];this.input.value=String(value);if(!silent)this.settings.onChange.call(this,String(value));}
        type(query){this.settings.onType.call(this,query);}
        load(query){const calls=[];this.settings.load.call(this,query,rows=>{calls.push(rows);for(const row of rows||[])this.addOption(row);});return calls;}
        destroy(){this.events.destroy();delete this.input.tomselect;}
    }
    const doc={createElement:()=>new Node(),createTextNode:text=>({textContent:text}),getElementById:()=>summary,
        addEventListener:(name,fn)=>events[name]=fn,querySelectorAll:()=>[element]};
    runInNewContext(code,{document:doc,window:{TomSelect:Picker,location:{origin:"http://local.test"}},URL,AbortController,
        fetch:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}))});
    events.DOMContentLoaded();
    return {element,summary,requests,picker:instances[0],events,async settle(){await new Promise(r=>setImmediate(r));}};
}
const result=(paper_id,pk=10)=>({ok:true,json:async()=>({results:[{paper_id,pk,title:"Example title",authors:"Example Author"}]})});
test("untouched initial value loads exact ID metadata, including PK-valued Editor picker",async()=>{
    for(const [initial,field] of [["P006","paper_id"],["6","pk"]]) {
        const p=setup(initial,field);p.requests[0].resolve(result("P006",6));await p.settle();
        assert.equal(p.element.value,initial);assert.equal(p.summary.hidden,false);
        assert.equal(p.requests[0].url.searchParams.get("selected_field"),field);
    }
});
test("late initial success cannot overwrite a new choice, even if transport ignores abort",async()=>{
    const p=setup();p.picker.type("P010");const calls=p.picker.load("P010");
    p.requests[1].resolve(result("P010"));await p.settle();p.picker.setValue("P010",false);
    assert.equal(p.requests[0].options.signal.aborted,true);
    p.requests[0].resolve(result("P006"));await p.settle();
    assert.equal(p.element.value,"P010");assert.equal(calls.length,1);
});
test("late initial failure cannot clear a user selection or its summary",async()=>{
    const p=setup();p.picker.addOption({paper_id:"P010"});p.picker.setValue("P010",false);
    p.requests[0].reject(new Error("offline"));await p.settle();
    assert.equal(p.element.value,"P010");assert.equal(p.summary.hidden,false);
});
test("an intentional clear or typing takes precedence over delayed initialization",async()=>{
    const p=setup();p.picker.type("");p.requests[0].resolve(result("P006"));await p.settle();
    assert.equal(p.element.value,"");assert.equal(p.summary.hidden,true);
});
test("obsolete searches balance loading callbacks but cannot add old options",async()=>{
    const p=setup("");p.picker.type("old");const old=p.picker.load("old");
    p.picker.type("new");const current=p.picker.load("new");
    p.requests[1].resolve(result("P010"));await p.settle();
    p.requests[0].resolve(result("P006"));await p.settle();
    assert.equal(current.length,1);assert.equal(old.length,1);assert.equal(old[0],undefined);
    assert.deepEqual(Object.keys(p.picker.options),["P010"]);
});
test("lookup failure is explicit, retry works, and no arbitrary ID is selected",async()=>{
    const p=setup();p.requests[0].resolve({ok:false});await p.settle();
    assert.match(p.summary.textContent,/could not be loaded/);assert.equal(p.element.value,"");
    p.picker.type("P010");p.picker.load("P010");p.requests[1].resolve(result("P010"));await p.settle();
    p.picker.setValue("P010",false);assert.equal(p.element.value,"P010");
});
test("destroy aborts both channels and late callbacks cannot touch the removed picker",async()=>{
    const p=setup();p.picker.type("P010");const calls=p.picker.load("P010");p.picker.destroy();
    assert.ok(p.requests.every(r=>r.options.signal.aborted));
    p.requests[0].resolve(result("P006"));p.requests[1].resolve(result("P010"));await p.settle();
    assert.equal(calls.length,0);assert.equal(p.element.value,"");
});
