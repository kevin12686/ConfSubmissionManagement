import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import {test} from 'node:test';
import assert from 'node:assert/strict';

const source = readFileSync(new URL('../../static/submissions/comparison_detail.js', import.meta.url), 'utf8');
function setup() {
    const timers = new Map(), requests = []; let id = 0;
    const window = {AbortController, createImageBitmap: async blob => blob,
        fetch: (url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject}))};
    runInNewContext(source, {window, setTimeout: (fn, ms) => {timers.set(++id,{fn,ms});return id;}, clearTimeout: id => timers.delete(id)});
    return {client:window.CFMComparisonDetail.create(),requests,
        tick(ms=160) {for(const [id,t] of timers) if(t.ms===ms){timers.delete(id);t.fn();}},
        async resolve(index, status=200) {
            const bitmap={width:800,height:300,closed:false,close(){this.closed=true;}};
            requests[index].resolve({ok:status===200,status,headers:{get:()=> 'image/png'},blob:async()=>bitmap});
            for(let i=0;i<8;i++) await Promise.resolve();
            return bitmap;
        }};
}
test('debounces movement, aborts superseded work and closes stale decoded pixels',async()=>{
    const p=setup(), seen=[];
    p.client.request('before','/one',b=>seen.push(b),()=>{});
    p.client.request('before','/two',b=>seen.push(b),()=>{});p.tick();
    assert.equal(p.requests.length,1);assert.equal(p.requests[0].url,'/two');
    p.client.request('before','/three',b=>seen.push(b),()=>{});
    assert.equal(p.requests[0].options.signal.aborted,true);
    const stale=await p.resolve(0);assert.equal(stale.closed,true);assert.equal(seen.length,0);
    p.tick();const current=await p.resolve(1);assert.equal(seen[0],current);
    p.client.dispose();assert.equal(current.closed,true);
});
test('same view uses latest overlay callback and reuses only the identical crop',async()=>{
    const p=setup(), seen=[];
    p.client.request('after','/exact',()=>seen.push('old'),()=>{});p.tick();
    p.client.request('after','/exact',()=>seen.push('current'),()=>{});
    const bitmap=await p.resolve(0);assert.deepEqual(seen,['current']);
    p.client.request('after','/exact',b=>assert.equal(b,bitmap),()=>{});p.tick();
    assert.equal(p.requests.length,1);
});
test('snapshot rejection is distinct from transient failure; hidden work cannot draw',async()=>{
    const p=setup(), errors=[];
    p.client.request('before','/expired',()=>assert.fail('must not draw'),e=>errors.push(e));p.tick();await p.resolve(0,409);
    assert.deepEqual(errors,[true]);
    p.client.request('after','/unavailable',()=>assert.fail('must not draw'),e=>errors.push(e));p.tick();await p.resolve(1,503);
    assert.deepEqual(errors,[true,false]);
    p.client.request('before','/late',()=>assert.fail('must not draw'),()=>{});p.tick();p.client.dispose();
    assert.equal((await p.resolve(2)).closed,true);
});
test('request timeout aborts without retry loops',async()=>{
    const p=setup();p.client.request('before','/slow',()=>{},()=>{});p.tick();p.tick(10000);
    assert.equal(p.requests[0].options.signal.aborted,true);assert.equal(p.requests.length,1);
    p.client.dispose();
});
