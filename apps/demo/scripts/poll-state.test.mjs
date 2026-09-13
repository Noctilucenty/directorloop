import {test} from 'node:test';
import assert from 'node:assert/strict';
import {decidePoll,repairProgress,workflowEventMessage} from '../src/poll-state.ts';
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

test('an older terminal first-pass report does not stop an active second evidence review',()=>{
 const d=decidePoll(ok({state:'RUNNING',stage:'SCREENING_REPAIR'}),ok([{stage:'SCREENING_REPAIR',message:'Workflow stage'}]),ok({status:'needs_review',review_repair:{status:'running',attempted_sections:4,completed_sections:1}}),4);
 assert.equal(d.stop,false);assert.equal(d.failures,0);assert.equal(d.phase,'Rechecking incomplete sections · 1 of 4 done');
 const stageOnly=decidePoll(ok({state:'RUNNING',stage:'SCREENING_REPAIR'}),ok([]),ok({status:'needs_review'}),0);
 assert.equal(stageOnly.stop,false);assert.equal(stageOnly.phase,'Rechecking incomplete sections');
});

test('the corrected terminal report finishes polling and repair does not hide real failures',()=>{
 const report={status:'complete',review_repair:{status:'complete',attempted_sections:4,completed_sections:4}};
 const finished=decidePoll(ok({state:'RUNNING',stage:'SCREENING_REPAIR'}),ok([]),ok(report),0);
 assert.equal(finished.stop,true);assert.equal(finished.phase,'Review complete');
 const failed=decidePoll(ok({state:'FAILED',stage:'SCREENING_REPAIR',error:'Second review could not finish'}),ok([]),ok({status:'needs_review',review_repair:{status:'running',attempted_sections:4,completed_sections:1}}),0);
 assert.equal(failed.stop,true);assert.equal(failed.error,'Second review could not finish');
});

test('an orphaned repair status keeps the existing bounded read-only recovery policy',()=>{
 const d=decidePoll(fail(500),fail(500),ok({status:'needs_review',review_repair:{status:'running',attempted_sections:3,completed_sections:1}}),4);
 assert.equal(d.stop,true);assert.equal(d.manual,true);assert.match(d.error,/will not start a new analysis/);
});

test('repair progress reports actual rechecks without claiming every result became valid',()=>{
 assert.equal(workflowEventMessage({stage:'SCREENING_REPAIR',message:'Workflow stage'}),'Rechecking incomplete sections');
 assert.equal(workflowEventMessage({stage:'SCREENING_REPAIR',message:'Checking section 3 again'}),'Checking section 3 again');
 assert.equal(repairProgress({status:'partial',attempted_sections:4,completed_sections:2}),'Second review · 2 of 4 sections rechecked');
 assert.equal(repairProgress({status:'running',attempted_sections:0,completed_sections:0}),'Rechecking incomplete sections');
 assert.equal(repairProgress({status:'complete',attempted_sections:2,completed_sections:3}),null);
});
