import {useState} from 'react';
import {summarizeReview,checkHasEvidence,type ReviewMoment as Moment} from './review-state';
const shortReason=(text:string)=>{
 const first=text.split(/(?<=[.!?])\s+/)[0]?.trim()||'No short finding available.';
 const words=first.split(/\s+/);
 return words.length<=18?first:words.slice(0,18).join(' ')+'…';
};
const evidenceNote=(issues:string[])=>issues.some(x=>x.startsWith('Intermediate checkpoint:'))?'This finding may confuse a checkpoint with the ending.':issues.some(x=>x.includes('pending future payoff'))?'An unseen payoff is not proof of a problem.':issues.some(x=>x.includes('checklist is incomplete'))?'The detailed review is incomplete.':issues.some(x=>x.includes('quote is absent'))?'A quoted line wasn’t found in the transcript.':issues.some(x=>x.includes('frame citation was not supplied'))?'A cited frame wasn’t supplied to the reviewer.':issues.some(x=>x.includes('no supporting observation')||x.includes('no anchored'))?'This estimate has no usable supporting observation.':issues.some(x=>x.includes('requires a verbatim quote'))?'A transcript claim is missing its quoted source.':'The evidence supporting this moment needs review.';
const seconds=(ms:number)=>Number((ms/1000).toFixed(1))+'s';
const timecode=(ms:number)=>String(Math.floor(ms/60000)).padStart(2,'0')+':'+String(Math.floor(ms/1000)%60).padStart(2,'0');
export default function RiskChart({windows,duration}:{windows:Moment[];duration:number}){
 const [selected,setSelected]=useState<string|null>(null);
 const {levels,preferred,gaps,state,checked}=summarizeReview(windows,duration);
 const selectedIndex=windows.findIndex(w=>w.start_ms+':'+w.end_ms===selected);
 const index=selectedIndex<0?preferred:selectedIndex, chosen=windows[index];
 const select=(i:number)=>setSelected(windows[i].start_ms+':'+windows[i].end_ms);
 const total=Math.max(duration,...windows.map(x=>x.end_ms),1), percent=(n:number)=>(n/total*100)+'%';
 const headline=state==='concern'?'Review '+timecode(windows[preferred].start_ms)+'–'+timecode(windows[preferred].end_ms)+' first.':state==='incomplete'?'The evidence needs checking.':state==='unknown'?'Not enough evidence to judge.':'No specific attention issue found.';
 const y=(risk:string)=>({high:26,medium:76,low:126}[risk]??76), height=166;
 const label=(i:number)=>i===0?'Opening':i===windows.length-1?'Ending':windows.length===3?'Early sequence':'Part '+(i+1);
 const ticks=windows.length>4?[0,total/4,total/2,total*3/4,total]:[...new Set([0,...windows.flatMap(w=>[w.start_ms,w.end_ms]),total])];
 return <section className="risk-overview editorial-risk" aria-label="Sampled attention risk">
  <p className="risk-kicker">Attention review</p>
  <h3>{headline}</h3>
  <p className="risk-intro">AI section risk · {checked}/{windows.length} evidence checks passed · not measured retention</p>
  <div className="risk-plot">
   <div className="risk-axis" aria-hidden="true"><span className="risk-high">High</span><span className="risk-medium">Medium</span><span className="risk-low">Low</span></div>
   <div className="risk-graph"><svg width="100%" height={height} role="img" aria-label={'AI estimated attention risk. '+windows.map((w,i)=>label(i)+' '+seconds(w.start_ms)+' to '+seconds(w.end_ms)+': '+levels[i]).join('. ')}>
    {[26,76,126].map(row=><line key={row} x1="0" x2="100%" y1={row} y2={row} stroke="var(--line-3)" strokeDasharray="2 6"/>)}
    {gaps.map(g=><g key={g.start}><rect x={percent(g.start)} y="5" width={percent(g.end-g.start)} height={height-30} fill="var(--surface-2)"/><line x1={percent(g.start)} x2={percent(g.start)} y1="5" y2={height-25} stroke="var(--line-3)"/><line x1={percent(g.end)} x2={percent(g.end)} y1="5" y2={height-25} stroke="var(--line-3)"/>{(g.end-g.start)/total>.2&&<text x={percent((g.start+g.end)/2)} y="81" textAnchor="middle" fill="var(--fg-3)" fontSize="12">Not reviewed</text>}</g>)}
    {windows.map((w,i)=><g key={w.end_ms} role="button" tabIndex={0} aria-label={label(i)+' '+seconds(w.start_ms)+' to '+seconds(w.end_ms)} onClick={()=>select(i)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();select(i)}}}>
     <rect className="risk-segment" x={percent(w.start_ms)} y={levels[i]==='unknown'?8:y(levels[i])-5} width={percent(w.end_ms-w.start_ms)} height={levels[i]==='unknown'?127:10} rx="2" fill={levels[i]==='unknown'||w.validation_issues.length>0?'none':i===index?'var(--accent)':'var(--fg-3)'} stroke={i===index?'var(--accent)':'var(--fg-3)'} strokeWidth={w.validation_issues.length>0||levels[i]==='unknown'?1.5:0} strokeDasharray={w.validation_issues.length>0||levels[i]==='unknown'?'3 3':undefined}/>
     <title>{label(i)+' · '+seconds(w.start_ms)+'–'+seconds(w.end_ms)+' · '+levels[i]}</title>
    </g>)}
    {ticks.map(t=><text key={t} x={percent(t)} y={height-2} textAnchor={t===0?'start':t===total?'end':'middle'} fill="var(--fg-3)" fontSize="12">{seconds(t)}</text>)}
   </svg></div>
  </div>
  {(gaps.length>0||windows.some(w=>w.validation_issues.length>0))&&<p className="risk-gap">{gaps.length>0&&gaps.map(g=>seconds(g.start)+'–'+seconds(g.end)).join(', ')+' was not reviewed.'}{windows.some(w=>w.validation_issues.length>0)&&' Dashed sections are unverified, not low risk.'}</p>}
  <div className={"risk-moments"+(windows.length>3?" many-moments":"")} role="group" aria-label="Choose a reviewed moment">{windows.map((w,i)=><button key={w.end_ms} type="button" className={i===index?'is-selected':''} aria-pressed={i===index} onClick={()=>select(i)}><span>{label(i)}</span><b>{timecode(w.start_ms)}–{timecode(w.end_ms)}</b><em>{levels[i]==='unknown'?'Unverified':levels[i]+' risk'}</em>{w.validation_issues.length>0&&<small>Check evidence</small>}</button>)}</div>
  {chosen&&<div className="risk-reason" aria-live="polite"><div className="reason-time"><span>{label(index)}</span><strong>{timecode(chosen.start_ms)}–{timecode(chosen.end_ms)}</strong></div><div className="reason-copy"><p>{chosen.validation_issues.length>0&&<small>Provisional finding · </small>}{chosen.judgment?(chosen.display_reason??shortReason(chosen.judgment.suggestion)):chosen.status==='pending'?'Waiting for this moment.':'No validated judgment for this moment.'}</p>{chosen.validation_issues.length>0&&<small>{evidenceNote(chosen.validation_issues)}</small>}</div></div>}
  {chosen?.judgment?.review_checks?.length? <details className="complete-evidence"><summary>What we checked in this section</summary><dl className="review-checklist">{chosen.judgment.review_checks.map(c=><div key={c.aspect}><dt>{c.aspect.replaceAll('_',' ')} <span>{!checkHasEvidence(c,chosen.validation_issues)?'Evidence missing':c.status==='unknown'?'Not assessed':c.status==='clear'?'No issue found':'Possible issue'}</span></dt><dd>{checkHasEvidence(c,chosen.validation_issues)?c.reason:'The supplied evidence does not support this check.'}</dd></div>)}</dl></details>:null}
 </section>;
}
