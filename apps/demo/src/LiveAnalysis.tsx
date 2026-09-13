import { useEffect, useRef, useState } from 'react';
import liveConfig from './live-config.json';
import replay from './screening-replay.json';
import RiskChart from './RiskChart';
import BrandMark from './BrandMark';
import {decidePoll} from './poll-state';
export type Source = File | {src:string; name:string} | null;
type Observation={text:string;kind:string;frame_timestamps_ms:number[];asr_quote:string|null};
type Window={start_ms:number;end_ms:number;status:string;validation_issues:string[];weave_url?:string;judgment:null|{understanding:string;attention_risk:string;suggestion:string;uncertainties:string[];observations:Observation[]}};
type Report={status:string;model_calls:number;weave_url?:string;windows:Window[];error?:string|null;duration_ms:number};
type Event={seq:number;stage:string;message:string};
type Job={state:string;stage:string;elapsed_ms:number;error?:string};
const ENDPOINT=liveConfig.endpoint;
class RequestError extends Error { constructor(message:string,public status?:number){super(message);} }
const takeaway=(text:string)=>text.split(/(?<=[.!?])\s+/).map(x=>x.trim()).find(x=>/[.!?]$/.test(x)&&x.length<=120&&x.split(/\s+/).length<=18)||'Review this moment before making a change.';
const timing=(w:Window,index:number,count:number)=>index===0?'Opening · 0–'+(w.end_ms/1000).toFixed(0)+'s':index===count-1?'Ending · '+(w.start_ms/1000).toFixed(0)+'–'+(w.end_ms/1000).toFixed(0)+'s':'By '+(w.end_ms/1000).toFixed(0)+'s';
const trace=(url?:string)=>url?.startsWith('https://wandb.ai/')?url:undefined;
export default function LiveAnalysis({source,mode,onBusy}:{source:Source;mode:'single'|'ab';onBusy:(value:boolean)=>void}){
 const [busy,setBusy]=useState(false),[phase,setPhase]=useState(''),[error,setError]=useState(''),[result,setResult]=useState<Report|null>(null),[job,setJob]=useState<Job|null>(null),[events,setEvents]=useState<Event[]>([]),[ids,setIds]=useState<{job_id:string;screen_id:string}|null>(null),[connected,setConnected]=useState<boolean|null>(null);
 const [analyzedName,setAnalyzedName]=useState(''),[isReplay,setIsReplay]=useState(false);
 const [pollPaused,setPollPaused]=useState(false),[pollAttempt,setPollAttempt]=useState(0);
 const submissionRef=useRef(false);
 useEffect(()=>onBusy(busy||pollPaused),[busy,pollPaused,onBusy]);
 const request=useRef<{source:Source;id:string}|null>(null);const panel=useRef<HTMLDivElement>(null);const session=useRef(crypto.randomUUID().replaceAll('-',''));
 useEffect(()=>{if(!ENDPOINT)return;void fetch(ENDPOINT+'/health').then(r=>r.json()).then(x=>setConnected(Boolean(x.available))).catch(()=>setConnected(false));},[]);
 async function api(path:string,init:RequestInit={}){
  const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),init.method==='POST'?120000:20000);
  try{const response=await fetch(ENDPOINT+path,{...init,signal:controller.signal,headers:{'X-Demo-Session':session.current,...init.headers}});
   const body=await response.json().catch(()=>null);
   if(!response.ok)throw new RequestError(typeof body?.detail==='string'?body.detail:'This request could not be completed.',response.status);
   if(body===null)throw new RequestError('The engine returned an unreadable response.');return body;
  }catch(e){if(controller.signal.aborted)throw new RequestError(init.method==='POST'?'The upload response timed out. Retry the same video to recover this submission.':'The progress check timed out.');throw e;}finally{clearTimeout(timeout);}
 }
 useEffect(()=>{
  if(!ids)return;let stopped=false;let timer:ReturnType<typeof setTimeout>|undefined;let failures=0;
  async function poll(){
   const [next,ev,report]=await Promise.allSettled([api('/jobs/'+ids!.job_id),api('/jobs/'+ids!.job_id+'/events'),api('/reports/'+ids!.screen_id)]);
   if(stopped)return;
   // Each response stands alone: a transient job error cannot erase saved findings.
   if(next.status==='fulfilled')setJob(next.value);
   if(ev.status==='fulfilled')setEvents(previous=>ev.value.length>=previous.length?ev.value:previous);
   if(report.status==='fulfilled')setResult(report.value);
   const decision=decidePoll(next,ev,report,failures);failures=decision.failures;
   setPhase(decision.phase);setError(decision.error);
   if(decision.stop){setBusy(false);setPollPaused(decision.manual);return;}
   if(!stopped)timer=setTimeout(()=>void poll(),decision.delay);
  }
  void poll();return()=>{stopped=true;if(timer)clearTimeout(timer);};
 },[ids,pollAttempt]);
 function checkProgress(){if(!ids)return;setPollPaused(false);setError('');setBusy(true);setPhase('Checking the existing review');setPollAttempt(value=>value+1);}
 async function start(){if(!source||mode!=='single'||busy||submissionRef.current||pollPaused)return;if(!ENDPOINT){setError('Live analysis is not connected yet.');return;}submissionRef.current=true;setPollPaused(false);setAnalyzedName(source.name);setIsReplay(false);setBusy(true);setError('');setResult(null);setEvents([]);setJob(null);setIds(null);setPhase('Uploading your video');setTimeout(()=>panel.current?.scrollIntoView({behavior:'auto',block:'start'}),50);
 try{let file:File;if(source instanceof File)file=source;else{const r=await fetch(source.src);if(!r.ok)throw new Error('The selected demo video is unavailable.');file=new File([await r.blob()],source.name,{type:'video/mp4'});}if(file.size>50*1024*1024)throw new Error('Live analysis supports files up to 50 MB.');if(request.current?.source!==source)request.current={source,id:crypto.randomUUID()};const form=new FormData();form.append('request_id',request.current.id);form.append('file',file);const response=await api('/analyze',{method:'POST',body:form});setPhase('Upload received. Waiting for the screening worker.');setIds(response);}catch(e){setError(e instanceof Error?e.message:'Analysis could not start.');setPhase('Analysis did not start');setBusy(false);}finally{submissionRef.current=false;}}
 async function cancel(){if(!ids)return;try{await api('/jobs/'+ids.job_id+'/cancel',{method:'POST'});setPhase('Cancellation requested. Waiting for the current step to finish.');if(pollPaused){setPollPaused(false);setBusy(true);setPollAttempt(value=>value+1);}}catch(e){setError(String(e));}}
 function replaySaved(){setPollPaused(false);setBusy(false);setIsReplay(true);setIds(null);setJob(null);setEvents([]);setError('');setResult(replay);setPhase('Review at a glance');setAnalyzedName(replay.source_title);setTimeout(()=>panel.current?.scrollIntoView({behavior:'auto',block:'start'}),50);}
 return <><div className="input-actions"><span className="muted">{mode==='ab'?'Live screening accepts one video. Explore the recorded A/B experiment below.':'Whole-video review · up to 8 steps'}</span><span className="input-buttons"><button className="btn btn-primary btn-hero" disabled={!source||mode!=='single'||busy||pollPaused} onClick={()=>void start()}>{busy?'Analyzing…':pollPaused?'Updates paused':'Analyze video'}</button><a className="btn btn-quiet" href="#experiment">Explore a demo</a></span></div><button type="button" className="text-link replay-review" disabled={busy||pollPaused} onClick={replaySaved}>View an example review</button>{connected===false&&!busy&&<p className="live-note">The live engine is currently unavailable. The recorded experiment remains ready to explore.</p>}
 {(phase||error||result)&&<div className={'live-analysis'+(busy?' is-running':'')+(result?' has-result':'')} ref={panel}><div className="live-analysis-head"><div><span className="eyebrow analysis-brand"><BrandMark/>{isReplay?'Recorded result · no new analysis':busy?'Live engine · W&B Inference':'Screening result'}</span>{(busy||!result)&&<h2>{phase}</h2>}{analyzedName&&<p className="live-source-name">{analyzedName}</p>}</div>{job&&<span className="mono">{Number.isFinite(job.elapsed_ms)?(job.elapsed_ms/1000).toFixed(1)+'s':'In progress'}</span>}</div><p className="live-note">AI suggestions. Review before editing.</p>{error&&<div className="callout callout-bad" role="alert">{error}</div>}<div className="live-status" role="status" aria-live="polite">{busy?phase:result?.status==='needs_review'?'Some evidence needs review.':job?.error||''}</div>{pollPaused&&<button type="button" className="btn btn-small" onClick={checkProgress}>Check progress</button>}{(busy||pollPaused)&&ids&&<button className="btn btn-small" onClick={()=>void cancel()}>Stop analysis</button>}
 {events.length>0&&<details className="live-progress" open={busy}><summary>Actual workflow progress · {events.length} events</summary><ol>{events.map(e=><li key={e.seq}><span className="mono">{String(e.seq).padStart(2,'0')}</span><span>{e.message||e.stage.replaceAll('_',' ').toLowerCase()}</span></li>)}</ol></details>}
 {result&&<><RiskChart windows={result.windows} duration={result.duration_ms}/><details className="complete-evidence"><summary>Evidence details and Weave trace</summary>{isReplay&&<p className="live-note">The example uses short editorial summaries of saved model suggestions. The original wording is preserved below.</p>}<div className="live-result-meta"><span>{result.model_calls} model calls</span><span>{result.duration_ms>0?(result.duration_ms/1000).toFixed(1)+'s video':'Preparing media'}</span>{trace(result.weave_url)&&<a href={trace(result.weave_url)} target="_blank" rel="noreferrer">Open live Weave trace ↗</a>}</div><div className="live-windows">{result.windows.map((w,index)=><article className="live-window compact-window" key={w.end_ms}><div className="live-window-head"><span className="eyebrow">{timing(w,index,result.windows.length)}</span><span className={`window-state state-${w.status}`}>{w.status==='needs_review'?'Review':w.status.replaceAll('_',' ')}</span></div>{w.judgment?<><h3>{takeaway(w.judgment.suggestion)}</h3><p className="risk-label">AI attention risk · <b>{w.judgment.attention_risk}</b></p></>:<h3>{w.status==='pending'?'Waiting for this moment.':'No validated judgment.'}</h3>}{w.validation_issues.length>0&&<p className="citation-warning">{w.validation_issues.length} citation {w.validation_issues.length===1?'check needs':'checks need'} review.</p>}<details className="screening-details"><summary>Details</summary>{w.judgment&&<><p>{w.judgment.understanding}</p><ul>{w.judgment.observations.map((o,i)=><li key={i}><span className="observation-kind">{o.kind.replaceAll('_',' ')}</span><p>{o.text}</p><small>{o.frame_timestamps_ms.length>0?'Frames: '+o.frame_timestamps_ms.map(t=>(t/1000).toFixed(2)+'s').join(', '):''}{o.asr_quote?'Transcript: “'+o.asr_quote+'”':''}</small></li>)}</ul>{w.judgment.suggestion&&<p className="review-suggestion"><b>Full suggestion</b> {w.judgment.suggestion}</p>}{w.judgment.uncertainties.map((u,i)=><p className="live-note" key={i}>{u}</p>)}</>}{w.validation_issues.length>0&&<ul>{w.validation_issues.map((v,i)=><li key={i}>{v}</li>)}</ul>}{trace(w.weave_url)&&<a className="text-link" href={trace(w.weave_url)} target="_blank" rel="noreferrer">Inspect this step ↗</a>}</details></article>)}</div></details></>}
 </div>}</>;
}
