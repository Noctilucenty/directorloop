import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {metricPresentation, metricExplanation, scoreDrivers, scoreSummaryState, supportedImprovements, supportedStrengths, SCORE_DISCLAIMER, SCORE_LABELS} from '../src/score-summary.ts';
import {reviewPriorityMoments} from '../src/review-state.ts';

const metric = (overrides = {}) => ({score: 75, coverage: 1, rated_ms: 12000, total_ms: 12000, rated_sections: 4, total_sections: 4, provisional: false, reason: 'Supported creative evidence.', ...overrides});
const card = (overrides = {}) => ({version: 'creative-potential-v1', status: 'complete', evidence_level: 'model_rubric', predicts_audience_outcomes: false, metrics: {creative: metric(), retention: metric(), virality: metric()}, improvements: [], method: 'Duration-weighted rubric.', limitations: [], ...overrides});
const improvement = (overrides = {}) => ({window_index: 0, start_ms: 0, end_ms: 2000, aspect: 'visual_clarity', action: 'Keep the product visible during the demonstration.', reason: 'The object is obscured by text.', observation_indices: [0], ...overrides});

test('a complete rating keeps the backend score and evidence denominator', () => {
  const result = metricPresentation(metric());
  assert.equal(result.score, 75);
  assert.equal(result.value, '75');
  assert.equal(result.provisional, false);
  assert.equal(result.sections, '4 of 4 sections rated');
  assert.equal(result.duration, '12.0 / 12.0s of evidence');
});

test('zero is a real supplied score, while null and invalid numbers are not rated', () => {
  assert.equal(metricPresentation(metric({score: 0})).value, '0');
  for (const score of [null, undefined, NaN, Infinity, -1, 101, '75']) {
    const result = metricPresentation(metric({score}));
    assert.equal(result.score, null);
    assert.equal(result.value, 'Not rated');
  }
});

test('partial duration, section or metric coverage remains visibly provisional', () => {
  for (const fields of [{coverage: .5, rated_ms: 6000}, {rated_sections: 2}, {provisional: true}]) {
    const result = metricPresentation(metric(fields));
    assert.equal(result.status, 'Provisional');
    assert.equal(result.value, '75');
  }
  assert.equal(metricPresentation(metric(), true).status, 'Provisional');
});

test('an asserted score without a usable evidence denominator cannot be displayed', () => {
  for (const fields of [{coverage: 0}, {coverage: 1.01}, {rated_ms: 0}, {total_ms: 1000}, {rated_sections: 0}, {total_sections: 2}]) {
    assert.equal(metricPresentation(metric(fields)).value, 'Not rated');
  }
});

test('running, failed and legacy reports do not fabricate or reveal a final score', () => {
  assert.equal(scoreSummaryState(card(), 'running').state, 'pending');
  assert.match(scoreSummaryState(card(), 'failed').message, /incomplete review/);
  assert.equal(scoreSummaryState(undefined, 'complete').message, 'Scores appear on new analyses.');
  assert.equal(scoreSummaryState(card({status: 'partial'}), 'needs_review').state, 'ready');
  assert.equal(scoreSummaryState(card({predicts_audience_outcomes: true}), 'complete').state, 'unavailable');
  assert.equal(scoreSummaryState(card({evidence_level: 'human'}), 'complete').state, 'unavailable');
});

test('improvements preserve backend ordering, qualifications and evidence without truncation', () => {
  const first = improvement({reason: 'The object may be difficult to identify while the large subtitle covers the demonstration.'});
  const second = improvement({window_index: 1, start_ms: 2000, end_ms: 5000, action: 'Hold the product shot until the label is readable.'});
  const source = [first, {...first}, improvement({action: 'Does the subtitle need changing?'}), second, improvement({window_index: 2}), improvement({window_index: 3})];
  const snapshot = structuredClone(source);
  const output = supportedImprovements(source);
  assert.equal(output.length, 3);
  assert.deepEqual(output[0], first);
  assert.deepEqual(output[1], second);
  assert.deepEqual(source, snapshot);
  for (const invalid of [{observation_indices: []}, {observation_indices: [-1]}, {start_ms: -1}, {end_ms: 0}, {action: 'Try a clearer…'}, {action: 'Does this video need clearer subtitles.'}, {reason: ''}]) {
    assert.equal(supportedImprovements([improvement(invalid)]).length, 0);
  }
});

test('the score strip labels rubric indices rather than probabilities or audience outcomes', () => {
  assert.equal(SCORE_DISCLAIMER, 'AI rubric · not audience predictions');
  assert.equal(SCORE_LABELS.retention, 'Retention potential');
  assert.equal(SCORE_LABELS.virality, 'Virality potential');
  const component = readFileSync(new URL('../src/ScoreSummary.tsx', import.meta.url), 'utf8');
  assert.ok(component.includes('/100'));
  assert.ok(!/viral chance|predicted completion|predicted views|percent of viewers/i.test(component));
  const integration = readFileSync(new URL('../src/LiveAnalysis.tsx', import.meta.url), 'utf8');
  assert.ok(integration.indexOf('<ScoreSummary ') < integration.indexOf('<RiskChart '));
});

test('evidence-backed improvements remain available when the report has no numeric ratings', () => {
  const scorecard = card({status: 'unavailable', metrics: {creative: metric({score: null}), retention: metric({score: null}), virality: metric({score: null})}, improvements: [improvement()]});
  assert.equal(scoreSummaryState(scorecard, 'needs_review').state, 'ready');
  assert.equal(supportedImprovements(scorecard.improvements).length, 1);
  assert.equal(metricPresentation(scorecard.metrics.creative).value, 'Not rated');
  const component = readFileSync(new URL('../src/ScoreSummary.tsx', import.meta.url), 'utf8');
  assert.ok(component.includes('const improvements = supportedImprovements(scorecard.improvements)'));
});

const driver=(overrides={})=>({window_index:0,start_ms:0,end_ms:2000,reason:'The opening names a clear question about the landscape.',observation_indices:[0],...overrides});
test('score explanations use admitted timestamped reasons and preserve their qualification',()=>{
 const supplied=metric({score:45,explanation:'The specific reason to share may be less clear than the visual appeal.',drivers:[driver()]});
 assert.equal(metricExplanation(supplied),supplied.explanation);
 assert.equal(scoreDrivers(supplied).length,1);
 assert.equal(metricExplanation(metric({explanation:'Does this work?',drivers:[driver()]})),driver().reason);
 assert.match(metricExplanation(metric({score:null,drivers:[]})),/Not enough supported evidence/);
 assert.match(metricExplanation(metric({explanation:'Unsupported claim.',drivers:[]})),/not recorded/);
});

test('strengths require real evidence references and valid times; empty feedback never invents weaknesses',()=>{
 const strength={...driver(),aspect:'visual_clarity'};
 assert.deepEqual(supportedStrengths([strength,{...strength}]),[strength]);
 for(const invalid of [{observation_indices:[]},{start_ms:-1},{end_ms:0},{reason:'Is the image clear?'},{reason:'The image is clear…'}]){
  assert.deepEqual(supportedStrengths([{...strength,...invalid}]),[]);
 }
 assert.deepEqual(scoreDrivers(metric({drivers:[driver({end_ms:13000})]})),[]);
 assert.deepEqual(supportedStrengths(),[]);
 const component=readFileSync(new URL('../src/ScoreSummary.tsx',import.meta.url),'utf8');
 assert.ok(component.includes('What’s working'));assert.ok(component.includes('What to improve'));
 assert.ok(component.includes('No specific edit is supported by the checked evidence.'));
 assert.ok(component.includes('metricExplanation(supplied)'));
});

const reviewWindows=()=>[0,1,2,3].map(index=>({start_ms:index*2000,end_ms:(index+1)*2000,status:'complete',validation_issues:[],judgment:{attention_risk:'low',suggestion:'The sampled scene is visible.',observations:[{text:'The walking shot stays on screen.',kind:'visible_fact',frame_timestamps_ms:[index*2000+500],asr_quote:null}]}}));
const reviewTimeline=(scores)=>scores.map((score,index)=>({window_index:index,start_ms:index*2000,end_ms:(index+1)*2000,score,reason:score===null?null:'The walking shot is static, which may hold less attention.',observation_indices:score===null?[]:[0]}));
test('review priorities preserve exact lower-rated reasons, bound the list, and keep zero as a real rating',()=>{
 const windows=reviewWindows(),timeline=reviewTimeline([50,0,25,50]),before=structuredClone(timeline);
 const result=reviewPriorityMoments(windows,timeline);
 assert.deepEqual(result.map(point=>point.score),[0,25]);
 assert.equal(result[0].reason,timeline[1].reason);assert.equal(result[0].start_ms,2000);
 assert.deepEqual(timeline,before);
});
test('strong, missing, final, and unsupported timeline ratings never create an improvement priority',()=>{
 const windows=reviewWindows();
 for(const scores of [[75,75,75,75],[null,null,null,null],[75,75,75,50]])assert.deepEqual(reviewPriorityMoments(windows,reviewTimeline(scores)),[]);
 const timeline=reviewTimeline([50,75,75,75]);
 assert.deepEqual(reviewPriorityMoments(windows,timeline.map(point=>({...point,observation_indices:[99]}))),[]);
 assert.deepEqual(reviewPriorityMoments(windows.map((w,index)=>index===0?{...w,status:'failed'}:w),timeline),[]);
 assert.deepEqual(reviewPriorityMoments(windows.map((w,index)=>index===0?{...w,attention_context:'last_endcard'}:w),timeline),[]);
 assert.deepEqual(reviewPriorityMoments(windows.map((w,index)=>index===0?{...w,validation_issues:['Observation 1: missing anchor']}:w),timeline),[]);
 const component=readFileSync(new URL('../src/ScoreSummary.tsx',import.meta.url),'utf8');
 assert.ok(component.includes('improvements.length ? [] : reviewPriorityMoments'));
 assert.ok(component.includes('Review this moment'));assert.ok(component.includes('Lower-rated moment; review before changing.'));
 assert.ok(component.includes('<p>{item.reason}</p>'));
});
