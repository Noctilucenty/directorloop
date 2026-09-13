import {test} from 'node:test';
import assert from 'node:assert/strict';
import {summarizeReview,checkHasEvidence} from '../src/review-state.ts';
const moment=(start,end,risk='low',issues=[])=>({start_ms:start,end_ms:end,status:issues.length?'needs_review':'complete',validation_issues:issues,judgment:{attention_risk:risk,suggestion:'A specific observation.'}});
test('all flagged low estimates are incomplete, never an all-clear',()=>{
 const result=summarizeReview([moment(0,2000,'low',['Checklist incomplete']),moment(2000,4000,'low',['Bad quote'])],4000);
 assert.equal(result.state,'incomplete');assert.equal(result.checked,0);assert.deepEqual(result.levels,['unknown','unknown']);
});
test('a properly supported flat review is preserved at short and long durations',()=>{
 for(const duration of [8000,90000]){
  const result=summarizeReview([moment(0,duration/2),moment(duration/2,duration)],duration);
  assert.equal(result.state,'clear');assert.deepEqual(result.levels,['low','low']);
 }
});
test('unsupported high estimate cannot outrank a supported concern',()=>{
 const result=summarizeReview([moment(0,2000,'high',['Missing observation']),moment(2000,4000,'medium')],4000);
 assert.equal(result.state,'concern');assert.equal(result.preferred,1);
});
test('unreviewed gaps and pending sections prevent an all-clear',()=>{
 assert.equal(summarizeReview([moment(0,2000),moment(6000,8000)],8000).state,'incomplete');
 assert.equal(summarizeReview([{...moment(0,2000),status:'pending'}],2000).state,'incomplete');
});
test('natural conclusion is not ranked as a repair target',()=>{
 const result=summarizeReview([moment(0,2000),{...moment(2000,4000,'high'),attention_context:'last_endcard'}],4000);
 assert.equal(result.state,'clear');assert.equal(result.preferred,0);
});
test('unsupported aspect or cited observation cannot be shown as clear',()=>{
 const check={aspect:'caption_alignment',status:'clear',reason:'Aligned',observation_indices:[1]};
 assert.equal(checkHasEvidence(check,['Review check caption_alignment: matching transcript evidence missing']),false);
 assert.equal(checkHasEvidence(check,['Observation 2: quote is absent']),false);
 assert.equal(checkHasEvidence(check,['Observation 1: quote is absent']),true);
});
