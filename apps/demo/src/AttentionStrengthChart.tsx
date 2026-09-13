import {useState} from 'react';
import {summarizeAttentionStrength,summarizeReview,sectionTime,checkHasEvidence,type ReviewMoment,type AttentionStrengthRating} from './review-state';
import {supportedImprovements,type ScoreImprovement} from './score-summary';

export default function AttentionStrengthChart({windows,duration,timeline,improvements=[]}:{windows:ReviewMoment[];duration:number;timeline:AttentionStrengthRating[];improvements?:ScoreImprovement[]}) {
 const [selected,setSelected]=useState<string|null>(null);
 const {points,preferred,checked,hasVariation}=summarizeAttentionStrength(windows,timeline);
 const {gaps}=summarizeReview(windows,duration);
 const found=windows.findIndex(w=>`${w.start_ms}:${w.end_ms}`===selected),index=found<0?preferred:found;
 const chosen=windows[index],point=points[index],total=Math.max(duration,...windows.map(w=>w.end_ms),1),height=166;
 const pct=(ms:number)=>`${ms/total*100}%`,y=(score:number)=>126-score;
 const label=(i:number)=>i===0?'Opening':i===windows.length-1?'Ending':`Part ${i+1}`;
 const select=(i:number)=>setSelected(`${windows[i].start_ms}:${windows[i].end_ms}`);
 const concerns=supportedImprovements(improvements).filter(item=>{
  const w=windows[item.window_index];return w&&item.start_ms===w.start_ms&&item.end_ms===w.end_ms&&
   item.observation_indices.every(i=>i<(w.judgment?.observations?.length??0))&&checkHasEvidence({aspect:item.aspect,status:'concern',reason:item.reason,observation_indices:item.observation_indices},w.validation_issues);
 });
 const ticks=[0,total/4,total/2,total*3/4,total];
 const headline=!checked?'Not enough evidence to rate attention.':hasVariation?`Attention is weakest at ${sectionTime(windows[preferred].start_ms)}–${sectionTime(windows[preferred].end_ms)}.`:'Similar attention strength across the rated sections.';
 return <section className="risk-overview editorial-risk" aria-label="AI attention strength">
  <p className="risk-kicker">Attention strength</p><h3>{headline}</h3>
  <p className="risk-intro">AI content ratings · {checked}/{windows.length} sections · higher is stronger · not audience retention %</p>
  <div className="risk-plot"><div className="risk-axis" aria-hidden="true"><span className="risk-high">Stronger</span><span className="risk-medium">Mixed</span><span className="risk-low">Weaker</span></div>
   <div className="risk-graph"><svg width="100%" height={height} role="group" aria-label={'AI content ratings. '+windows.map((w,i)=>`${label(i)} ${sectionTime(w.start_ms)} to ${sectionTime(w.end_ms)}: ${points[i]?.score??'not rated'} out of 100`).join('. ')}>
    {[26,76,126].map(row=><line key={row} x1="0" x2="100%" y1={row} y2={row} stroke="var(--line-3)" strokeDasharray="2 6"/>)}
    {gaps.map(g=><rect key={g.start} x={pct(g.start)} y="8" width={pct(g.end-g.start)} height="127" fill="var(--surface-2)"/>)}
    {windows.map((w,i)=>{const rating=points[i],previous=points[i-1],concern=concerns.find(item=>item.window_index===i);return <g key={w.end_ms} className="risk-segment" role="button" tabIndex={0} aria-pressed={i===index} aria-label={`${label(i)}, ${rating?rating.score+' out of 100':'not rated'}`} onClick={()=>select(i)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();select(i);}}}>
      <rect className="strength-hit-target" x={pct(w.start_ms)} y="8" width={pct(w.end_ms-w.start_ms)} height="127" fill="transparent" pointerEvents="all"/>
      {rating&&previous&&windows[i-1].end_ms===w.start_ms&&<line x1={pct(w.start_ms)} x2={pct(w.start_ms)} y1={y(previous.score)} y2={y(rating.score)} stroke="var(--accent)" strokeWidth="2"/>}
      <rect x={pct(w.start_ms)} y={rating?y(rating.score)-4:8} width={pct(w.end_ms-w.start_ms)} height={rating?8:127} rx="2" fill={rating?(i===index?'var(--accent)':'var(--fg-3)'):'none'} stroke={rating?'none':'var(--fg-3)'} strokeDasharray={rating?undefined:'3 3'}/>
      {rating&&concern&&<circle cx={pct((w.start_ms+w.end_ms)/2)} cy={y(rating.score)-12} r="3" fill="var(--warn)"><title>{concern.action}</title></circle>}
      <title>{rating?rating.reason:'No supported content rating for this section.'}</title>
    </g>;})}
    {ticks.map(t=><text key={t} x={pct(t)} y={height-2} textAnchor={t===0?'start':t===total?'end':'middle'} fill="var(--fg-3)" fontSize="12">{Number((t/1000).toFixed(1))}s</text>)}
   </svg></div>
  </div>
  {(checked<windows.length||gaps.length>0)&&<p className="risk-gap">Dashed sections have no supported rating. Gaps are not filled in.</p>}
  {concerns.length>0&&<p className="risk-gap">Dots mark evidence-backed review ideas.</p>}
  <div className={'risk-moments'+(windows.length>3?' many-moments':'')} role="group" aria-label="Choose an attention rating">{windows.map((w,i)=><button key={w.end_ms} type="button" className={i===index?'is-selected':''} aria-pressed={i===index} onClick={()=>select(i)}><span>{label(i)}</span><b>{sectionTime(w.start_ms)}–{sectionTime(w.end_ms)}</b><em>{points[i]?`${points[i]!.score}/100 strength`:'Not rated'}</em></button>)}</div>
  {chosen&&<div className="risk-reason" aria-live="polite"><div className="reason-time"><span>{label(index)}</span><strong>{sectionTime(chosen.start_ms)}–{sectionTime(chosen.end_ms)}</strong></div><div className="reason-copy"><p><small className="finding-label">{point?`AI content rating · ${point.score}/100`:'Evidence unavailable'}</small>{point?.reason??'This section has no supported attention-strength rating.'}</p></div></div>}
  {point&&<details className="complete-evidence"><summary>Evidence for this rating</summary><p className="live-note">Section {index+1} · Observation{point.observation_indices.length===1?'':'s'} {point.observation_indices.map(i=>i+1).join(', ')}. Original risk judgments and checks remain in Evidence details below.</p></details>}
 </section>;
}
