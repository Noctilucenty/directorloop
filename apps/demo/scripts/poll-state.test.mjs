import {test} from 'node:test';
import assert from 'node:assert/strict';
import {decidePoll} from '../src/poll-state.ts';
const ok=value=>({status:'fulfilled',value});
const fail=status=>({status:'rejected',reason:{status}});
test('saved final report stops polling even when job/events fail',()=>{
 const d=decidePoll(fail(500),fail(404),ok({status:'needs_review'}),4);
 assert.equal(d.stop,true);assert.equal(d.manual,false);assert.equal(d.error,'');
});
test('saved failure retains its precise error',()=>{
 const d=decidePoll(ok({state:'RUNNING'}),fail(500),ok({status:'failed',error:'Provider quota reached'}),0);
 assert.equal(d.stop,true);assert.equal(d.error,'Provider quota reached');
});
test('a temporarily absent first report does not stop a healthy job',()=>{
 const d=decidePoll(ok({state:'RUNNING'}),ok([{message:'Extracting frames'}]),fail(404),4);
 assert.equal(d.stop,false);assert.equal(d.failures,0);assert.equal(d.phase,'Extracting frames');
});
test('five failed cycles stop automatic polling with manual GET recovery',()=>{
 let d;for(let n=0;n<5;n++)d=decidePoll(fail(404),fail(500),fail(404),n);
 assert.equal(d.stop,true);assert.equal(d.manual,true);assert.match(d.error,/existing review/);
});
test('completed job waits boundedly for report; auxiliary events failure cannot hide a result',()=>{
 const d=decidePoll(ok({state:'COMPLETED'}),fail(500),fail(404),4);
 assert.equal(d.manual,true);
 const recovered=decidePoll(fail(500),fail(500),ok({status:'complete'}),0);
 assert.equal(recovered.phase,'Review complete');assert.equal(recovered.stop,true);
});
test('permanent access failure stops automatic checks immediately',()=>{
 for(const status of [401,403]){
  const d=decidePoll(fail(status),ok([]),fail(404),0);
  assert.equal(d.stop,true);assert.equal(d.manual,false);assert.match(d.error,/access/);
 }
});
test('partial running report with failing status is preserved but not polled forever',()=>{
 const d=decidePoll(fail(500),ok([]),ok({status:'running'}),4);
 assert.equal(d.manual,true);
});
