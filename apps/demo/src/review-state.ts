export type ReviewCheck = {aspect:string;status:string;reason:string;observation_indices?:number[]};
export type ReviewObservation = {text:string;kind:string;frame_timestamps_ms:number[];asr_quote:string|null};
export type AttentionAssessment = {version:'attention-evidence-v1';status:'supported'|'blocked';risk:'low'|'medium'|'high'|'unknown';reason:string;excluded_checks:string[];semantic_grounding_verified:false};
export type ReviewMoment = {attention_context?:string;attention_assessment?:AttentionAssessment|null;display_reason?:string;start_ms:number;end_ms:number;status:string;validation_issues:string[];judgment:null|{attention_risk:string;suggestion:string;review_checks?:ReviewCheck[];observations?:ReviewObservation[]}};
export function attentionAssessment(w:ReviewMoment):AttentionAssessment|null {
 const assessment=w.attention_assessment;
 if(!['complete','needs_review'].includes(w.status)||!w.judgment||!assessment||assessment.version!=='attention-evidence-v1'||
  assessment.semantic_grounding_verified!==false||!['supported','blocked'].includes(assessment.status)||
  typeof assessment.reason!=='string'||!assessment.reason.trim()||assessment.reason.length>640||
  !Array.isArray(assessment.excluded_checks)||assessment.excluded_checks.length>32||
  assessment.excluded_checks.some(check=>typeof check!=='string'||!check.trim()||check.length>200)||
  (assessment.status==='supported'?!['low','medium','high'].includes(assessment.risk):assessment.risk!=='unknown'))return null;
 return assessment;
}
/** A scoped attention decision can exclude unrelated checks without erasing their failures. */
export const reviewedRisk = (w:ReviewMoment) => {
 if(w.attention_assessment!==undefined){const assessment=attentionAssessment(w);return assessment?.status==='supported'?assessment.risk:'unknown';}
 const risk=w.judgment?.attention_risk;
 return w.status==='complete'&&!w.validation_issues.length&&risk&&['low','medium','high'].includes(risk)?risk:'unknown';
};
const rank=(risk:string)=>({high:3,medium:2,low:1}[risk]??0);
/** Evidence failures cannot become a reassuring all-clear or a ranked repair. */
export function summarizeReview(windows:ReviewMoment[],duration:number) {
 const levels=windows.map(reviewedRisk);
 const actionable=windows.map((w,i)=>w.attention_context?.startsWith('last_')?'unknown':levels[i]);
 const preferred=actionable.reduce((best,risk,i)=>rank(risk)>rank(actionable[best])?i:best,0);
 let cursor=0;const gaps:{start:number;end:number}[]=[];
 for(const w of [...windows].sort((a,b)=>a.start_ms-b.start_ms)) {
  if(w.start_ms>cursor)gaps.push({start:cursor,end:w.start_ms});
  cursor=Math.max(cursor,w.end_ms);
 }
 if(cursor<duration)gaps.push({start:cursor,end:duration});
 const incomplete=gaps.length>0 || levels.some(risk=>risk==='unknown');
 const state=windows.length===0?'unknown':actionable.some(risk=>rank(risk)>=2)?'concern':incomplete?'incomplete':actionable.every(risk=>risk==='unknown')?'unknown':'clear';
 return {levels,preferred,gaps,state,checked:levels.filter(risk=>risk!=='unknown').length};
}
export function checkHasEvidence(check:ReviewCheck,issues:string[]) {
 return !issues.some(issue=>issue.startsWith('Review check '+check.aspect+':') ||
  (check.observation_indices??[]).some(i=>issue.startsWith('Observation '+(i+1)+':')) || issue.includes('checklist is incomplete'));
}

/** Preserve a complete model sentence and its uncertainty; never truncate a claim. */
export function declarativeFinding(text?:string):string|null {
 const value=text?.trim();
 if(!value || value.length>320 || /[?？؟]|\.\.\.|…/.test(value) ||
    /^(?:does|do|did|is|are|was|were|can|could|would|should|will|has|have)\b/i.test(value) ||
    !/[.!。！]["”’']?$/.test(value))return null;
 return value;
}
export const sectionTime=(ms:number)=>{
 const tenths=Math.round(Math.max(0,Number.isFinite(ms)?ms:0)/100);
 return String(Math.floor(tenths/600)).padStart(2,'0')+':'+String(Math.floor(tenths%600/10)).padStart(2,'0')+'.'+tenths%10;
};
export const sectionRange=(w:ReviewMoment)=>sectionTime(w.start_ms)+'–'+sectionTime(w.end_ms);
export const riskLabel=(w:ReviewMoment)=>w.status==='pending'?'Waiting':w.status==='failed'?'No result':w.status==='not_attempted'?'Not reviewed':reviewedRisk(w)==='unknown'?'Unverified':reviewedRisk(w)+' risk';
export const aspectLabel=(aspect:string)=>({pacing:'Pace',visual_clarity:'Visual clarity',caption_readability:'Subtitles',caption_alignment:'Caption wording',hook_and_payoff:'Hook & payoff',tone_from_words:'Wording & tone',voice_delivery:'Voice delivery',share_motivation:'Reason to share'}[aspect]??aspect.replaceAll('_',' '));
function anchoredCheck(w:ReviewMoment,c:ReviewCheck) {
 const refs=c.observation_indices??[], observations=w.judgment?.observations??[];
 return refs.length>0 && refs.every(i=>Number.isInteger(i)&&i>=0&&i<observations.length) && checkHasEvidence(c,w.validation_issues) &&
  !(w.validation_issues.some(x=>x.startsWith('Intermediate checkpoint:')) && c.aspect==='hook_and_payoff');
}
export function findingForMoment(w:ReviewMoment):{label:string;text:string} {
 const checks=w.judgment?.review_checks??[];
 for(const status of ['concern','clear']) {
  for(const c of checks) {
   const text=declarativeFinding(c.reason);
   if(c.status===status && text && anchoredCheck(w,c))return {label:status==='concern'?'Possible issue · '+aspectLabel(c.aspect):'Observed · '+aspectLabel(c.aspect),text};
  }
 }
 // Legacy reviews may have no aspect checklist. Keep their original evidence visible.
 for(const [i,o] of (w.judgment?.observations??[]).entries()) {
  const text=declarativeFinding(o.text);
  if(text && ['visible_fact','caption_claim','asr_claim'].includes(o.kind) &&
    (o.frame_timestamps_ms.length>0||Boolean(o.asr_quote)) &&
    !w.validation_issues.some(x=>x.startsWith('Observation '+(i+1)+':')))return {label:'Observed in this section',text};
 }
 if(!checks.length && reviewedRisk(w)!=='unknown') {
  const text=declarativeFinding(w.judgment?.suggestion);
  if(text)return {label:'Model finding',text};
 }
 return {label:w.status==='pending'?'In progress':'Limited evidence',text:w.status==='pending'?'This section is waiting to be reviewed.':'No supported finding is available for this section.'};
}
