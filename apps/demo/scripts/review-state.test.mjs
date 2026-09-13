import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {summarizeReview,summarizeAttentionStrength,checkHasEvidence,attentionAssessment,reviewedRisk} from '../src/review-state.ts';
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

test('pending and empty reviews are not presented as completed judgments',()=>{
 assert.equal(summarizeReview([],0).state,'unknown');
 assert.equal(riskLabel({...moment(0,2000),status:'pending'}),'Waiting');
 assert.equal(riskLabel({...moment(0,2000),status:'failed'}),'No result');
 assert.equal(riskLabel({...moment(0,2000),status:'not_attempted'}),'Not reviewed');
});

const assessment=(overrides={})=>({version:'attention-evidence-v1',status:'supported',risk:'medium',reason:'The attention estimate cites the visible scene change.',excluded_checks:['caption_alignment'],semantic_grounding_verified:false,...overrides});
test('a scoped supported attention estimate survives an unrelated caption check without clearing its failure',()=>{
 const w={...moment(0,4000,'medium',['Review check caption_alignment: matching transcript evidence missing']),attention_assessment:assessment()};
 const original=structuredClone(w);
 assert.equal(reviewedRisk(w),'medium');assert.equal(riskLabel(w),'medium risk');
 const summary=summarizeReview([w],4000);assert.equal(summary.checked,1);assert.equal(summary.state,'concern');
 assert.deepEqual(attentionAssessment(w).excluded_checks,['caption_alignment']);
 assert.deepEqual(w,original);assert.equal(w.status,'needs_review');assert.equal(w.validation_issues.length,1);
});

test('an explicit blocked assessment cannot fall back to a reassuring raw complete result',()=>{
 const w={...moment(0,4000,'low'),attention_assessment:assessment({status:'blocked',risk:'unknown',reason:'The cited observation does not support the attention estimate.'})};
 assert.equal(reviewedRisk(w),'unknown');assert.equal(riskLabel(w),'Unverified');
 assert.equal(summarizeReview([w],4000).checked,0);
});

test('invalid attention contracts stay unknown even when the legacy raw record looks complete',()=>{
 for(const value of [null,assessment({version:'future'}),assessment({semantic_grounding_verified:true}),assessment({semantic_grounding_verified:undefined}),assessment({status:'verified'}),assessment({risk:'unknown'}),assessment({risk:'very high'}),assessment({status:'blocked',risk:'low'}),assessment({reason:''}),assessment({reason:null}),assessment({excluded_checks:'caption_alignment'}),assessment({excluded_checks:[null]})]){
  const w={...moment(0,4000,'low'),attention_assessment:value};
  assert.equal(attentionAssessment(w),null);assert.equal(reviewedRisk(w),'unknown');
 }
});

test('failed, pending, missing-judgment and unattempted sections cannot use a supplied supported assessment',()=>{
 for(const status of ['failed','pending','running','not_attempted','canceled']){
  const w={...moment(0,4000),status,attention_assessment:assessment()};
  assert.equal(reviewedRisk(w),'unknown');assert.equal(attentionAssessment(w),null);
 }
 assert.equal(reviewedRisk({...moment(0,4000),judgment:null,attention_assessment:assessment()}),'unknown');
});

test('legacy reviews retain strict gating and the chart does not call mixed checks a universal pass',()=>{
 assert.equal(reviewedRisk(moment(0,4000,'low')),'low');
 assert.equal(reviewedRisk(moment(0,4000,'low',['Unverified caption'])),'unknown');
 const chart=readFileSync(new URL('../src/RiskChart.tsx',import.meta.url),'utf8');
 assert.ok(chart.includes('evidence-linked estimates'));
 assert.ok(!chart.includes('sections passed evidence checks'));
 assert.ok(chart.includes("strokeDasharray={levels[i]==='unknown'?'3 3':undefined}"));
 assert.ok(chart.includes('Excluded from this estimate:'));
 assert.ok(chart.includes('not measured audience retention'));
});

test('a supported medium attention summary precedes an unrelated clear pacing check',()=>{
 const w={...supported(),attention_assessment:assessment()};
 w.judgment.suggestion='The interval provides a clear scripture reading and a specific app recommendation for a targeted audience.';
 w.judgment.review_checks=[{aspect:'pacing',status:'clear',reason:'The pacing stays steady and logical.',observation_indices:[0]}];
 assert.deepEqual(findingForMoment(w),{label:'AI attention estimate',text:w.judgment.suggestion});
 w.judgment.review_checks.push({aspect:'hook_and_payoff',status:'concern',reason:'The app pitch may feel abrupt after the tutorial.',observation_indices:[0]});
 assert.equal(findingForMoment(w).label,'Possible issue · Hook & payoff');
});

test('the attention-summary preference preserves question, missing-source and blocked safeguards',()=>{
 const base={...supported(),attention_assessment:assessment()};
 base.judgment.review_checks=[{aspect:'pacing',status:'clear',reason:'The pacing stays steady and logical.',observation_indices:[0]}];
 for(const w of [base,{...base,judgment:{...base.judgment,suggestion:'The topic changes…'}},
  {...base,attention_assessment:assessment({status:'blocked',risk:'unknown'}),judgment:{...base.judgment,suggestion:'The interval switches to a product recommendation.'}},
  {...base,validation_issues:['Attention explanation references a missing observation'],judgment:{...base.judgment,suggestion:'The interval switches to a product recommendation.'}}]){
  assert.notEqual(findingForMoment(w).label,'AI attention estimate');
 }
});

test('attention strength shows actual rubric differences even when categorical risk is flat',()=>{
 const windows=[0,1,2].map(i=>({...moment(i*2000,(i+1)*2000,'low'),judgment:{...moment(0,2000).judgment,observations:[evidence()]}}));
 const timeline=[75,50,75].map((score,i)=>({window_index:i,start_ms:i*2000,end_ms:(i+1)*2000,score,reason:i===1?'The walking shot is static and may hold less attention.':'The next visual detail supports the explanation.',observation_indices:[0]}));
 const result=summarizeAttentionStrength(windows,timeline);
 assert.deepEqual(result.points.map(point=>point?.score),[75,50,75]);assert.equal(result.preferred,1);assert.equal(result.hasVariation,true);
 assert.equal(result.points[result.preferred].reason,timeline[1].reason);
 assert.deepEqual(summarizeReview(windows,6000).levels,['low','low','low']);
 assert.equal(summarizeAttentionStrength(windows,timeline.map(point=>({...point,score:75}))).hasVariation,false);
});

test('missing, failed or malformed section ratings stay empty without invented peaks',()=>{
 const w={...moment(0,2000),judgment:{...moment(0,2000).judgment,observations:[evidence()]}};
 const point={window_index:0,start_ms:0,end_ms:2000,score:0,reason:'The intended subject is not visible in the sampled frames.',observation_indices:[0]};
 assert.equal(summarizeAttentionStrength([w],[point]).points[0].score,0);
 for(const invalid of [{score:null,reason:null,observation_indices:[]},{score:60},{end_ms:3000},{observation_indices:[9]},{reason:'Does this lose attention?'}]){
  assert.equal(summarizeAttentionStrength([w],[{...point,...invalid}]).checked,0);
 }
 assert.equal(summarizeAttentionStrength([{...w,status:'failed'}],[point]).checked,0);
 assert.equal(summarizeAttentionStrength([{...w,validation_issues:['Observation 1: quote is absent']}],[point]).checked,0);
 assert.equal(summarizeAttentionStrength([w],[point,{...point}]).checked,0);
 const chart=readFileSync(new URL('../src/AttentionStrengthChart.tsx',import.meta.url),'utf8');
 assert.ok(chart.includes('Attention strength'));assert.ok(chart.includes('not audience retention %'));
 assert.ok(chart.includes('point?.reason'));assert.ok(!chart.includes('findingForMoment'));
});

test('chart sections have full-height pointer targets and expose keyboard-selected state',()=>{
 const chart=readFileSync(new URL('../src/AttentionStrengthChart.tsx',import.meta.url),'utf8');
 assert.match(chart,/<rect className="strength-hit-target"[^>]+height="127"[^>]+fill="transparent" pointerEvents="all"/);
 assert.ok(chart.includes('aria-pressed={i===index}'));
 assert.ok(chart.includes("e.key==='Enter'||e.key===' '"));
 assert.match(chart,/<svg[^>]+role="group"/);
});
