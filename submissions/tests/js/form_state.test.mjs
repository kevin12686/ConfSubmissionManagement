import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";
import {runInNewContext} from "node:vm";
function load(name){const module={exports:{}};runInNewContext(readFileSync(new URL(`../../static/submissions/${name}.js`,import.meta.url),"utf8"),{module});return module.exports;}
const formState=load("form_state"),{createGuard}=load("form_guard");
test("effective control values, not input events, decide dirty state",()=>{
    const fields=[{type:"text",value:"original"},{type:"textarea",value:"notes"},{type:"checkbox",checked:true},
        {type:"radio",checked:false},{type:"file",files:[]},{type:"hidden",value:"signed-evidence"},
        {type:"text",value:"disabled",disabled:true},{type:"submit",value:"save"}];
    const state=formState.capture({querySelectorAll:()=>fields});assert.equal(state.controls.length,5);
    fields[0].value="changed";fields[2].checked=false;fields[4].files=[{name:"same.pdf",size:8,lastModified:1}];
    assert.equal(state.changed().length,3);
    fields[0].value="original";fields[2].checked=true;fields[4].files=[];fields[5].value="new-signature";
    assert.equal(state.changed().length,0);
});
test("radio restoration, select and notes reset remove all dirty values",()=>{
    const fields=[{type:"radio",checked:true},{type:"radio",checked:false},{type:"select-one",value:"pending"},{type:"textarea",value:""}];
    const state=formState.capture({querySelectorAll:()=>fields});
    fields[0].checked=false;fields[1].checked=true;fields[2].value="review_ok";fields[3].value="x";
    assert.equal(state.changed().length,4);
    fields[0].checked=true;fields[1].checked=false;fields[2].value="pending";fields[3].value="";
    assert.equal(state.changed().length,0);
});
test("cancelled navigation retains protection; accepted navigation asks once",()=>{
    let dirty=true,accept=false,confirms=0;
    const guard=createGuard({changed:()=>dirty?["notes"]:[]},()=>{confirms++;return accept;});
    assert.equal(guard.confirm(),false);assert.equal(guard.shouldBlockUnload(),true);
    accept=true;assert.equal(guard.confirm(),true);assert.equal(guard.shouldBlockUnload(),false);
    assert.equal(guard.confirm(),true);assert.equal(confirms,2);
    guard.resume();assert.equal(guard.shouldBlockUnload(),true);
    dirty=false;assert.equal(guard.confirm(),true);assert.equal(guard.shouldBlockUnload(),false);assert.equal(confirms,2);
});
test("own submit and page-history return do not disable future dirty protection",()=>{
    const guard=createGuard({changed:()=>["file"]},()=>true);
    guard.submitting();assert.equal(guard.shouldBlockUnload(),false);
    guard.resume();assert.equal(guard.shouldBlockUnload(),true);
});
