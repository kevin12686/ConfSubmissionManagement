import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";
import {runInNewContext} from "node:vm";
const code=readFileSync(new URL("../../static/submissions/worklist_state.js",import.meta.url),"utf8");
test("worklist replacement restores dirty siblings but not newly persisted values or acted-on drafts",()=>{
    const owner={id:"row-1",contains:field=>field.id==="own",getBoundingClientRect:()=>({top:100,bottom:400})};
    const sibling={id:"row-2",getBoundingClientRect:()=>({top:500,bottom:600})};
    const fields=[{id:"own",value:"submit",defaultValue:""},{id:"dirty",value:"unsaved",defaultValue:""},
        {id:"changed-server",value:"old draft",defaultValue:"old"},{id:"clean",value:"same",defaultValue:"same"}];
    const root={querySelectorAll:key=>key==="tbody[id]"?[owner,sibling]:key==="textarea[id]"?fields:
        key.includes("aria-expanded")?[{getAttribute:()=>"panel"}]:[{id:"panel",classList:{contains:()=>false}}]};
    const module={exports:{}}, scroll=[];
    const newFields=new Map([["dirty",{value:"",defaultValue:""}],["changed-server",{value:"new approval",defaultValue:"new approval"}],
        ["row-2",{getBoundingClientRect:()=>({top:300})}], ["panel",{classList:{add:()=>{}}}]]);
    runInNewContext(code,{module});const state=module.exports.capture(root,{closest:()=>owner});
    assert.deepEqual(Array.from(state.drafts,d=>d.id),["dirty","changed-server"]);
    assert.deepEqual(Array.from(state.expanded),["panel"]); // Also retain a panel still in its opening transition.
    const nextRoot={contains:()=>true,querySelectorAll:()=>[]};
    runInNewContext(code,{module,document:{getElementById:id=>newFields.get(id),addEventListener(){}},
        window:{scrollBy:options=>scroll.push(options)}});
    module.exports.restore(nextRoot,state);
    assert.equal(newFields.get("dirty").value,"unsaved");
    assert.equal(newFields.get("changed-server").value,"new approval");
    assert.equal(scroll[0].top,200); // Row removed: next visible row takes its place.
});
test("a sibling draft leaving the current page survives subsequent HTMX pagination",()=>{
    const handlers={},module={exports:{}},nodes=new Map();
    const doc={addEventListener:(name,fn)=>handlers[name]=fn,getElementById:id=>nodes.get(id)};
    const root=(fields)=>({id:"worklist",matches:()=>true,contains:()=>true,scrollIntoView(){},
        querySelectorAll:key=>key==="textarea[id]"?fields:[]});
    const original=root([{id:"reason-other",value:"unsaved note",defaultValue:""}]);
    runInNewContext(code,{module,document:doc,window:{scrollBy(){}}});
    function swap(before,after,verb,fields=[]) {
        const xhr={};
        handlers['htmx:beforeSwap']({detail:{target:before,shouldSwap:true,xhr,
            requestConfig:{verb,elt:{closest:()=>({closest:()=>null})}}}});
        nodes.clear();nodes.set("worklist",after);
        for(const field of fields) nodes.set(field.id,field);
        handlers['htmx:afterSettle']({detail:{xhr}});
    }
    const empty=root([]);
    swap(original,empty,"post");
    const returned={id:"reason-other",value:"",defaultValue:""};
    swap(empty,root([returned]),"get",[returned]);
    assert.equal(returned.value,"unsaved note");
    // Restoring the loaded value is intentional: do not resurrect an old draft.
    returned.value="";
    swap(root([returned]),empty,"get");
    const again={id:"reason-other",value:"",defaultValue:""};
    swap(empty,root([again]),"get",[again]);
    assert.equal(again.value,"");
});
test("typing in a sibling textarea retains focus and selection after the save response",()=>{
    const module={exports:{}},selections=[],focus=[];
    const field={id:"note",value:"ongoing draft",defaultValue:"",matches:()=>true,
        selectionStart:3,selectionEnd:7,selectionDirection:"backward"};
    const next={id:"note",value:"",defaultValue:"",focus:options=>focus.push(options),
        setSelectionRange:(...args)=>selections.push(args)};
    const root={contains:()=>true,querySelectorAll:key=>key==="textarea[id]"?[field]:[],scrollIntoView(){}};
    runInNewContext(code,{module,document:{activeElement:field,addEventListener(){},getElementById:()=>next},window:{}});
    const state=module.exports.capture(root,null);
    module.exports.restore(root,state,false);
    assert.equal(next.value,"ongoing draft");assert.equal(focus[0].preventScroll,true);
    assert.deepEqual(selections[0],[3,7,"backward"]);
});
