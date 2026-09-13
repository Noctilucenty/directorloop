export type ReviewCheck = {aspect:string;status:string;reason:string;observation_indices?:number[]};
export type ReviewMoment = {attention_context?:string;display_reason?:string;start_ms:number;end_ms:number;status:string;validation_issues:string[];judgment:null|{attention_risk:string;suggestion:string;review_checks?:ReviewCheck[]}};
export const reviewedRisk = (w:ReviewMoment) => w.status==='complete' && !w.validation_issues.length ? w.judgment?.attention_risk ?? 'unknown' : 'unknown';
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
 const state=actionable.some(risk=>rank(risk)>=2)?'concern':incomplete?'incomplete':actionable.every(risk=>risk==='unknown')?'unknown':'clear';
 return {levels,preferred,gaps,state,checked:levels.filter(risk=>risk!=='unknown').length};
}
export function checkHasEvidence(check:ReviewCheck,issues:string[]) {
 return !issues.some(issue=>issue.startsWith('Review check '+check.aspect+':') ||
  (check.observation_indices??[]).some(i=>issue.startsWith('Observation '+(i+1)+':')) || issue.includes('checklist is incomplete'));
}
