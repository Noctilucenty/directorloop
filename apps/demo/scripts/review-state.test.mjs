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

const {findingForMoment,declarativeFinding,sectionRange,riskLabel}=await import('../src/review-state.ts');
const evidence=(text='The speaker switches from a tutorial to an app demonstration.')=>({text,kind:'visible_fact',frame_timestamps_ms:[5000],asr_quote:null});
const supported=(risk='medium',issues=[])=>({...moment(4000,8823,risk,issues),judgment:{attention_risk:risk,suggestion:'Does the topic change feel too abrupt for the viewer?',observations:[evidence()],review_checks:[{aspect:'hook_and_payoff',status:'concern',reason:'The app pitch may feel abrupt after the tutorial.',observation_indices:[0]}]}});
test('questions never become findings; supported alternatives preserve the original qualification',()=>{
 const w=supported();const original=structuredClone(w);
 assert.equal(findingForMoment(w).text,'The app pitch may feel abrupt after the tutorial.');
 assert.deepEqual(w,original);
 for(const question of ['Does the change work?', 'What happens next?', 'Does the change work.', 'Can this be clearer.', 'هل هذا واضح؟', 'Is it clear…'])assert.equal(declarativeFinding(question),null);
});
test('complete long finding is not cut mid-sentence or stripped of its qualifier',()=>{
 const text='The video introduces a product after a long explanation, which may surprise someone who was expecting the promised demonstration.';
 assert.equal(declarativeFinding(text),text);
 assert.equal(declarativeFinding('The video introduces a product after a long explanation, which may'),null);
 assert.equal(declarativeFinding('字幕は読みやすい。'),'字幕は読みやすい。');
});
test('an invalid quote cannot produce a concern or a reassuring risk in either surface',()=>{
 const w=supported('medium',['Observation 1: quote is absent from the supplied ASR prefix']);
 assert.equal(findingForMoment(w).label,'Limited evidence');
 assert.equal(riskLabel(w),'Unverified');assert.equal(summarizeReview([w],8823).levels[0],'unknown');
});
test('unrelated invalid evidence does not erase a supported specific observation',()=>{
 const w=supported('medium',['Observation 2: quote is absent from the supplied ASR prefix']);
 assert.match(findingForMoment(w).text,/may feel abrupt/);assert.equal(riskLabel(w),'Unverified');
});
test('pending-payoff criticism at an intermediate checkpoint falls back to actual observations',()=>{
 const w=supported('medium',['Intermediate checkpoint: wording may mistake the review cutoff for the video ending']);
 assert.equal(findingForMoment(w).text,w.judgment.observations[0].text);
 assert.equal(riskLabel(w),'Unverified');
});
test('empty, failed, question-only and malformed-reference responses have safe fallbacks',()=>{
 const w=supported();w.judgment.review_checks[0].observation_indices=[99];w.judgment.observations=[];
 assert.equal(findingForMoment(w).label,'Limited evidence');
 assert.equal(findingForMoment({...w,status:'pending',judgment:null}).text,'This section is waiting to be reviewed.');
 assert.equal(findingForMoment({...w,status:'failed',judgment:null}).label,'Limited evidence');
 w.judgment.review_checks[0].observation_indices=[];
 assert.equal(findingForMoment(w).label,'Limited evidence');
});
test('all result surfaces share decimal boundaries without misleading floor/ceiling labels',()=>{
 assert.equal(sectionRange(moment(4000,8823)),'00:04.0–00:08.8');
 assert.equal(sectionRange(moment(8823,13645)),'00:08.8–00:13.6');
 assert.equal(sectionRange(moment(59999,61555)),'01:00.0–01:01.6');
});
