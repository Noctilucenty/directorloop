import {useState} from 'react';
type Moment={display_reason?:string;start_ms:number;end_ms:number;status:string;validation_issues:string[];judgment:null|{attention_risk:string;suggestion:string}};
const shortReason=(text:string)=>text.split(/(?<=[.!?])\s+/).map(x=>x.trim()).find(x=>/[.!?]$/.test(x)&&x.length<=120&&x.split(/\s+/).length<=18)||'Review this moment before making a change.';
const seconds=(ms:number)=>Number((ms/1000).toFixed(1))+'s';
const timecode=(ms:number)=>String(Math.floor(ms/60000)).padStart(2,'0')+':'+String(Math.floor(ms/1000)%60).padStart(2,'0');
export default function RiskChart({windows,duration}:{windows:Moment[];duration:number}){
 const [selected,setSelected]=useState(0);
 const index=Math.min(selected,Math.max(0,windows.length-1)), chosen=windows[index];
 const total=Math.max(duration,...windows.map(x=>x.end_ms),1), percent=(n:number)=>(n/total*100)+'%';
 const levels=windows.map(w=>w.judgment?.attention_risk??'unknown'), unknown=levels.includes('unknown');
 const headline=levels.includes('high')?'A moment worth reviewing.':levels.includes('medium')?'Some moments may lose attention.':levels.every(x=>x==='unknown')?'Not enough evidence to judge.':unknown?'No clear drop-off in reviewed moments.':'No clear drop-off flagged.';
 const gaps:{start:number;end:number}[]=[];let cursor=0;
 for(const w of windows){if(w.start_ms>cursor)gaps.push({start:cursor,end:w.start_ms});cursor=Math.max(cursor,w.end_ms);}
 if(cursor<total)gaps.push({start:cursor,end:total});
 const y=(risk:string)=>({high:26,medium:76,low:126}[risk]??76), height=166;
 const label=(i:number)=>i===0?'Opening':i===windows.length-1?'Ending':'Early sequence';
 const ticks=[...new Set([0,...windows.flatMap(w=>[w.start_ms,w.end_ms]),total])];
 return <section className="risk-overview editorial-risk" aria-label="Sampled attention risk">
  <p className="risk-kicker">Attention review</p>
  <h3>{headline}</h3>
  <p className="risk-intro">AI estimate · {windows.length} sampled moments</p>
  <div className="risk-plot">
   <div className="risk-axis" aria-hidden="true"><span className="risk-high">High</span><span className="risk-medium">Medium</span><span className="risk-low">Low</span></div>
   <div className="risk-graph"><svg width="100%" height={height} role="img" aria-label={'AI estimated attention risk. '+windows.map((w,i)=>label(i)+' '+seconds(w.start_ms)+' to '+seconds(w.end_ms)+': '+levels[i]).join('. ')}>
    {[26,76,126].map(row=><line key={row} x1="0" x2="100%" y1={row} y2={row} stroke="#39372f" strokeDasharray="2 6"/>)}
    {gaps.map(g=><g key={g.start}><rect x={percent(g.start)} y="5" width={percent(g.end-g.start)} height={height-30} fill="#1c1c18"/><line x1={percent(g.start)} x2={percent(g.start)} y1="5" y2={height-25} stroke="#38362e"/><line x1={percent(g.end)} x2={percent(g.end)} y1="5" y2={height-25} stroke="#38362e"/>{(g.end-g.start)/total>.2&&<text x={percent((g.start+g.end)/2)} y="81" textAnchor="middle" fill="#bbb5a8" fontSize="12">Not reviewed</text>}</g>)}
    {windows.map((w,i)=><g key={w.end_ms} role="button" tabIndex={0} aria-label={label(i)+' '+seconds(w.start_ms)+' to '+seconds(w.end_ms)} onClick={()=>setSelected(i)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();setSelected(i)}}}>
     <rect className="risk-segment" x={percent(w.start_ms)} y={levels[i]==='unknown'?8:y(levels[i])-5} width={percent(w.end_ms-w.start_ms)} height={levels[i]==='unknown'?127:10} rx="2" fill={levels[i]==='unknown'||w.validation_issues.length>0?'none':i===index?'#d3b47f':'#938f84'} stroke={i===index?'#d3b47f':'#938f84'} strokeWidth={w.validation_issues.length>0||levels[i]==='unknown'?1.5:0} strokeDasharray={w.validation_issues.length>0||levels[i]==='unknown'?'3 3':undefined}/>
     <title>{label(i)} · {seconds(w.start_ms)}–{seconds(w.end_ms)} · {levels[i]}</title>
    </g>)}
    {ticks.map(t=><text key={t} x={percent(t)} y={height-2} textAnchor={t===0?'start':t===total?'end':'middle'} fill="#c2bbad" fontSize="12">{seconds(t)}</text>)}
   </svg></div>
  </div>
  {gaps.length>0&&<p className="risk-gap">{gaps.map(g=>seconds(g.start)+'–'+seconds(g.end)).join(', ')} was not reviewed.{windows.some(w=>w.validation_issues.length>0)&&' Dashed marks need an evidence check.'}</p>}
  <div className="risk-moments" role="group" aria-label="Choose a reviewed moment">{windows.map((w,i)=><button key={w.end_ms} type="button" className={i===index?'is-selected':''} aria-pressed={i===index} onClick={()=>setSelected(i)}><span>{label(i)}</span><b>{timecode(w.start_ms)}–{timecode(w.end_ms)}</b><em>{levels[i]==='unknown'?'Not scored':levels[i]+' risk'}</em>{w.validation_issues.length>0&&<small>Check evidence</small>}</button>)}</div>
  {chosen&&<div className="risk-reason" aria-live="polite"><div className="reason-time"><span>{label(index)}</span><strong>{timecode(chosen.start_ms)}–{timecode(chosen.end_ms)}</strong></div><div className="reason-copy"><p>{chosen.judgment?(chosen.display_reason??shortReason(chosen.judgment.suggestion)):chosen.status==='pending'?'Waiting for this moment.':'No validated judgment for this moment.'}</p>{chosen.validation_issues.length>0&&<small>This moment needs an evidence check.</small>}</div></div>}
 </section>;
}
