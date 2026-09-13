import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {REVIEW_RECOVERY_KEY,parseReviewRecovery,readReviewRecovery,writeReviewRecovery,parseReviewRecoveryFragment,consumeReviewRecoveryFragment,uploadRecoveryDecision} from '../src/review-recovery.ts';
import {decidePoll} from '../src/poll-state.ts';
const saved = (overrides={}) => ({version:1,session:'a'.repeat(32),analysis:{job_id:'job_1a09c147f41_83bb2a07',screen_id:'screen_1a09c147f41_83bb2a07',name:'My video.mp4'},...overrides});
const memory = () => {const map=new Map();return {getItem:key=>map.get(key)??null,setItem:(key,value)=>map.set(key,value),removeItem:key=>map.delete(key),map};};
const fulfilled = value => ({status:'fulfilled',value});
const fragment = record => '#restore='+Buffer.from(JSON.stringify(record),'utf8').toString('base64url');

test('refresh preserves the exact capability and IDs for both running and completed jobs',()=>{
  const storage=memory();const record=saved();assert.equal(writeReviewRecovery(storage,record),true);
  for(const status of ['running','complete','needs_review']){
    const recovered=readReviewRecovery(storage);assert.deepEqual(recovered,record);
    const decision=decidePoll(fulfilled({state:status==='running'?'RUNNING':'COMPLETED',stage:'review'}),fulfilled([]),fulfilled({status}),0);
    assert.equal(decision.stop,status!=='running');
    assert.deepEqual(readReviewRecovery(storage),record);
  }
});

test('new analysis replaces the previous job without retaining a File, URL or response payload',()=>{
  const storage=memory();writeReviewRecovery(storage,saved());
  const cleared=saved({analysis:null});assert.equal(writeReviewRecovery(storage,cleared),true);assert.deepEqual(readReviewRecovery(storage),cleared);
  const next=saved({analysis:{job_id:'job_abc_def',screen_id:'screen_abc_def',name:'Next.mp4',src:'blob:old',file:{body:'private media'}}});
  writeReviewRecovery(storage,{...next,response:{windows:[]}});
  const recovered=readReviewRecovery(storage);
  assert.deepEqual(Object.keys(recovered),['version','session','analysis']);
  assert.deepEqual(Object.keys(recovered.analysis),['job_id','screen_id','name']);
  assert.equal(recovered.analysis.name,'Next.mp4');
});

test('malformed, mismatched, oversized and unknown-version storage cannot start recovery',()=>{
  for(const raw of ['not json','null',JSON.stringify(saved({version:2})),JSON.stringify(saved({session:'A'.repeat(32)})),JSON.stringify(saved({analysis:{job_id:'job_abc_def',screen_id:'screen_abc_123',name:'Clip.mp4'}})),JSON.stringify(saved({analysis:{job_id:'../jobs',screen_id:'screen_abc_def',name:'Clip.mp4'}})),JSON.stringify(saved({analysis:{job_id:'job_abc_def',screen_id:'screen_abc_def',name:'x'.repeat(513)}})), 'x'.repeat(4097)]){
    const storage=memory();storage.setItem(REVIEW_RECOVERY_KEY,raw);
    assert.equal(parseReviewRecovery(raw),null);assert.equal(readReviewRecovery(storage),null);assert.equal(storage.getItem(REVIEW_RECOVERY_KEY),null);
  }
});

test('blocked storage fails safely and a failed replacement removes the stale job when possible',()=>{
  const blocked={getItem(){throw Error('blocked');},setItem(){throw Error('blocked');},removeItem(){throw Error('blocked');}};
  assert.equal(readReviewRecovery(blocked),null);assert.equal(writeReviewRecovery(blocked,saved()),false);assert.equal(readReviewRecovery(null),null);
  const storage=memory();writeReviewRecovery(storage,saved());storage.setItem=()=>{throw Error('quota');};
  assert.equal(writeReviewRecovery(storage,saved({analysis:null})),false);assert.equal(readReviewRecovery(storage),null);
});

test('a valid recovery fragment is consumed once, sanitizes the URL, and persists only existing-job identity',()=>{
  const storage=memory(),record=saved({analysis:{job_id:'job_abc_def',screen_id:'screen_abc_def',name:'字幕 vídeo.mp4'}}),replaced=[];
  const hash=fragment(record);assert.deepEqual(parseReviewRecoveryFragment(hash),record);
  assert.deepEqual(consumeReviewRecoveryFragment({hash,pathname:'/demo',search:'?theme=blue'},url=>replaced.push(url),storage),record);
  assert.deepEqual(replaced,['/demo?theme=blue#']);assert.deepEqual(readReviewRecovery(storage),record);
  assert.equal(consumeReviewRecoveryFragment({hash:'#',pathname:'/demo',search:''},url=>replaced.push(url),storage),null);
  assert.equal(replaced.length,1);
});

test('invalid or arbitrary fragments never gain a capability or change existing saved access',()=>{
  const storage=memory();const record=saved();writeReviewRecovery(storage,record);
  for(const hash of ['#restore','#restore=%%%','#restore=bm90LWpzb24',fragment({url:'https://example.com',callback:'submit'}),fragment(saved({analysis:null})),fragment(saved({analysis:{job_id:'job_abc_def',screen_id:'screen_abc_123',name:'Wrong.mp4'}}))]){
    const replaced=[];assert.equal(parseReviewRecoveryFragment(hash),null);
    assert.equal(consumeReviewRecoveryFragment({hash,pathname:'/',search:''},url=>replaced.push(url),storage),null);
    assert.deepEqual(replaced,['/#']);assert.deepEqual(readReviewRecovery(storage),record);
  }
  const replaced=[];assert.equal(consumeReviewRecoveryFragment({hash:'#experiment',pathname:'/',search:''},url=>replaced.push(url),storage),null);assert.equal(replaced.length,0);
});

test('restored jobs use the existing GET poller with bounded failure handling, never an automatic POST',()=>{
  const source=readFileSync(new URL('../src/LiveAnalysis.tsx',import.meta.url),'utf8');
  assert.ok(source.includes('initialReviewRecovery()'));
  assert.ok(source.includes('session=useRef(recovery.session)'));
  assert.ok(source.includes("api('/jobs/'+ids!.job_id)"));
  assert.ok(source.includes("api('/reports/'+ids!.screen_id)"));
  const pollEffect=source.slice(source.indexOf('if(!ids)return;let stopped=false'),source.indexOf('function checkProgress()'));
  assert.ok(!/POST|\/analyze|start\(/.test(pollEffect));
  assert.ok(pollEffect.includes('decidePoll('));
  const recovery=readFileSync(new URL('../src/review-recovery.ts',import.meta.url),'utf8');
  assert.ok(recovery.includes('window.sessionStorage'));assert.ok(!/localStorage|fetch\(|console\./.test(recovery));
});

test('a refresh during upload retains its request ID and resolves to the original submitted job',()=>{
  const storage=memory();const pending={request_id:'12345678-1234-1234-1234-123456789abc',name:'Original.mp4'};
  const record=saved({analysis:null,pending_request:pending});writeReviewRecovery(storage,record);
  assert.deepEqual(readReviewRecovery(storage),record);
  assert.equal(uploadRecoveryDecision({state:'pending'},1,0).kind,'waiting');
  assert.equal(uploadRecoveryDecision({state:'unknown'},2,0).kind,'waiting');
  const decision=uploadRecoveryDecision({state:'submitted',job_id:saved().analysis.job_id,screen_id:saved().analysis.screen_id},3,0);
  assert.equal(decision.kind,'submitted');
  writeReviewRecovery(storage,saved({analysis:{...decision.ids,name:pending.name}}));
  assert.equal(readReviewRecovery(storage).pending_request,undefined);
  assert.equal(readReviewRecovery(storage).analysis.job_id,saved().analysis.job_id);
});

test('unknown upload confirmation is bounded; mismatched IDs cannot redirect recovered polling',()=>{
  assert.equal(uploadRecoveryDecision({state:'unknown'},30,0).kind,'paused');
  assert.equal(uploadRecoveryDecision(null,5,5).kind,'paused');
  assert.equal(uploadRecoveryDecision({state:'failed'},1,0).kind,'failed');
  assert.equal(uploadRecoveryDecision({state:'submitted',job_id:'job_abc_def',screen_id:'screen_abc_123'},1,0).kind,'waiting');
  assert.equal(parseReviewRecovery(JSON.stringify(saved({analysis:null,pending_request:{request_id:'../../analyze',name:'Wrong.mp4'}}))),null);
});

test('the request ID is saved before POST and explicit same-upload retry reuses that ID',()=>{
  const source=readFileSync(new URL('../src/LiveAnalysis.tsx',import.meta.url),'utf8');
  assert.ok(source.indexOf('rememberUpload({request_id:request.current.id')<source.indexOf("api('/analyze',{method:'POST'"));
  assert.ok(source.includes('if(recoverRequest)request.current={source,id:recoverRequest.request_id}'));
  assert.ok(source.includes('source.name!==recoverRequest.name'));
  assert.ok(source.includes('onBusy(busy||(pollPaused&&!pendingUpload))'));
  const effect=source.slice(source.indexOf('if(!pendingUpload)return;'),source.indexOf('function checkProgress()'));
  assert.ok(effect.includes("api('/requests/'+pendingUpload!.request_id)"));
  assert.ok(!/POST|\/analyze|start\(/.test(effect));
});
