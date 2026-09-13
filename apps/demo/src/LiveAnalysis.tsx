import { useEffect, useRef, useState } from 'react';
import liveConfig from './live-config.json';
import replay from './screening-replay.json';
import RiskChart from './RiskChart';
import BrandMark from './BrandMark';
import ScoreSummary from './ScoreSummary';
import type {Scorecard} from './score-summary';
import {findingForMoment,sectionRange,riskLabel,type ReviewCheck} from './review-state';
import {decidePoll} from './poll-state';
import {availabilityNotice} from './availability-state';
import {initialReviewRecovery,writeReviewRecovery,reviewRecoveryStorage,uploadRecoveryDecision,type ReviewRecovery,type PendingUpload} from './review-recovery';
export type Source = File | {src:string; name:string} | null;
type Observation={text:string;kind:string;frame_timestamps_ms:number[];asr_quote:string|null};
type Window={start_ms:number;end_ms:number;status:string;validation_issues:string[];weave_url?:string;judgment:null|{understanding:string;attention_risk:string;suggestion:string;uncertainties:string[];observations:Observation[];review_checks?:ReviewCheck[]}};
type Report={status:string;model_calls:number;weave_url?:string;windows:Window[];error?:string|null;duration_ms:number;scorecard?:Scorecard|null};
type Event={seq:number;stage:string;message:string};
type Job={state:string;stage:string;elapsed_ms:number;error?:string};
const ENDPOINT=liveConfig.endpoint;
class RequestError extends Error { constructor(message:string,public status?:number){super(message);} }
const trace=(url?:string)=>url?.startsWith('https://wandb.ai/')?url:undefined;
export default function LiveAnalysis({source,mode,onBusy}:{source:Source;mode:'single'|'ab';onBusy:(value:boolean)=>void}){
 const [recovery]=useState<ReviewRecovery>(()=>initialReviewRecovery()??{version:1,session:crypto.randomUUID().replaceAll('-',''),analysis:null});
 const [busy,setBusy]=useState(Boolean(recovery.analysis||recovery.pending_request)),[phase,setPhase]=useState(recovery.analysis?'Recovering your saved review':recovery.pending_request?'Recovering your upload':''),[error,setError]=useState(''),[result,setResult]=useState<Report|null>(null),[job,setJob]=useState<Job|null>(null),[events,setEvents]=useState<Event[]>([]),[ids,setIds]=useState<{job_id:string;screen_id:string}|null>(recovery.analysis?{job_id:recovery.analysis.job_id,screen_id:recovery.analysis.screen_id}:null),[connected,setConnected]=useState<boolean|null>(null);
 const [analyzedName,setAnalyzedName]=useState(recovery.analysis?.name??recovery.pending_request?.name??''),[isReplay,setIsReplay]=useState(false),[restored,setRestored]=useState(Boolean(recovery.analysis||recovery.pending_request)),[recoveryUnavailable,setRecoveryUnavailable]=useState(()=>recovery.analysis||recovery.pending_request?!writeReviewRecovery(reviewRecoveryStorage(),recovery):false);
 const [pendingUpload,setPendingUpload]=useState<PendingUpload|null>(recovery.pending_request??null),[uploadRecoveryAttempt,setUploadRecoveryAttempt]=useState(0);
 const [pollPaused,setPollPaused]=useState(false),[pollAttempt,setPollAttempt]=useState(0);
 const [healthReason,setHealthReason]=useState<string|null>(null),[healthReachable,setHealthReachable]=useState<boolean|null>(null),[checkingAvailability,setCheckingAvailability]=useState(false);
 const availabilitySequence=useRef(0),healthController=useRef<AbortController|null>(null);
 const submissionRef=useRef(false);
 useEffect(()=>onBusy(busy||(pollPaused&&!pendingUpload)),[busy,pollPaused,pendingUpload,onBusy]);
 const request=useRef<{source:Source;id:string}|null>(null);const panel=useRef<HTMLDivElement>(null);const session=useRef(recovery.session);
 function rememberAnalysis(analysis:ReviewRecovery['analysis']){setRecoveryUnavailable(!writeReviewRecovery(reviewRecoveryStorage(),{version:1,session:session.current,analysis}));}
 function rememberUpload(pending_request:PendingUpload){setRecoveryUnavailable(!writeReviewRecovery(reviewRecoveryStorage(),{version:1,session:session.current,analysis:null,pending_request}));}
 async function checkAvailability(){
  const sequence=++availabilitySequence.current;healthController.current?.abort();
  if(!ENDPOINT){setConnected(false);setHealthReason('Live analysis is not configured.');setHealthReachable(false);return;}
  const controller=new AbortController();healthController.current=controller;setCheckingAvailability(true);
  const timeout=setTimeout(()=>controller.abort(),20000);
  try{const response=await fetch(ENDPOINT+'/health',{method:'GET',signal:controller.signal});const body=await response.json().catch(()=>null);
   if(sequence!==availabilitySequence.current)return;const available=response.ok&&body?.available===true;
   setConnected(available);setHealthReachable(true);setHealthReason(available?null:typeof body?.reason==='string'?body.reason:null);
  }catch{if(sequence===availabilitySequence.current){setConnected(false);setHealthReachable(false);setHealthReason(null);}}
  finally{clearTimeout(timeout);if(sequence===availabilitySequence.current)setCheckingAvailability(false);}
 }
 useEffect(()=>{void checkAvailability();return()=>{availabilitySequence.current++;healthController.current?.abort();};},[]);
 async function api(path:string,init:RequestInit={}){
  const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),init.method==='POST'?120000:20000);
  try{const response=await fetch(ENDPOINT+path,{...init,signal:controller.signal,headers:{'X-Demo-Session':session.current,...init.headers}});
   const body=await response.json().catch(()=>null);
   if(!response.ok)throw new RequestError(typeof body?.detail==='string'?body.detail:'This request could not be completed.',response.status);
   if(body===null)throw new RequestError('The engine returned an unreadable response.');availabilitySequence.current++;healthController.current?.abort();setCheckingAvailability(false);setConnected(true);setHealthReachable(true);setHealthReason(null);return body;
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
 useEffect(()=>{
  if(!pendingUpload)return;let stopped=false;let timer:ReturnType<typeof setTimeout>|undefined;let checks=0;let failures=0;
  async function recoverUpload(){
   let response=null;try{response=await api('/requests/'+pendingUpload!.request_id);failures=0;}
   catch(e){failures++;if(e instanceof RequestError&&[401,403].includes(e.status??0)){if(!stopped){setBusy(false);setPollPaused(false);setError('This page can no longer access the original upload.');}return;}}
   if(stopped)return;const decision=uploadRecoveryDecision(response,++checks,failures);setPhase(decision.message);
   if(decision.kind==='submitted'){rememberAnalysis({...decision.ids,name:pendingUpload!.name});setPendingUpload(null);setIds(decision.ids);setError('');return;}
   if(decision.kind==='failed'){rememberAnalysis(null);setPendingUpload(null);setBusy(false);setPollPaused(false);setError(decision.message);return;}
   if(decision.kind==='paused'){setBusy(false);setPollPaused(true);setError(decision.message);return;}
   timer=setTimeout(()=>void recoverUpload(),4000);
  }
  void recoverUpload();return()=>{stopped=true;if(timer)clearTimeout(timer);};
 },[pendingUpload,uploadRecoveryAttempt]);
 function checkProgress(){if(!ids&&!pendingUpload)return;setPollPaused(false);setError('');setBusy(true);if(pendingUpload){setPhase('Checking the original upload');setUploadRecoveryAttempt(value=>value+1);}else{setPhase('Checking the existing review');setPollAttempt(value=>value+1);}}
 async function start(recoverRequest?:PendingUpload){if(!source||mode!=='single'||busy||submissionRef.current||(pollPaused&&!recoverRequest))return;if(!ENDPOINT){setError('Live analysis is not connected yet.');return;}if(recoverRequest&&source.name!==recoverRequest.name){setError('Choose the same file, '+recoverRequest.name+', to retry this upload.');return;}if(recoverRequest)request.current={source,id:recoverRequest.request_id};submissionRef.current=true;rememberAnalysis(null);setPendingUpload(null);setRestored(false);setPollPaused(false);setAnalyzedName(source.name);setIsReplay(false);setBusy(true);setError('');setResult(null);setEvents([]);setJob(null);setIds(null);setPhase('Uploading your video');setTimeout(()=>panel.current?.scrollIntoView({behavior:'auto',block:'start'}),50);
 try{let file:File;if(source instanceof File)file=source;else{const r=await fetch(source.src);if(!r.ok)throw new Error('The selected demo video is unavailable.');file=new File([await r.blob()],source.name,{type:'video/mp4'});}if(file.size>50*1024*1024)throw new Error('Live analysis supports files up to 50 MB.');if(request.current?.source!==source)request.current={source,id:crypto.randomUUID()};const form=new FormData();form.append('request_id',request.current.id);form.append('file',file);rememberUpload({request_id:request.current.id,name:source.name.slice(0,512)});const response=await api('/analyze',{method:'POST',body:form});rememberAnalysis({job_id:response.job_id,screen_id:response.screen_id,name:source.name.slice(0,512)});setPhase('Upload received. Waiting for the screening worker.');setIds(response);}catch(e){setError(e instanceof Error?e.message:'Analysis could not start.');setPhase('Analysis did not start');setBusy(false);}finally{submissionRef.current=false;}}
 async function cancel(){if(!ids)return;try{await api('/jobs/'+ids.job_id+'/cancel',{method:'POST'});setPhase('Cancellation requested. Waiting for the current step to finish.');if(pollPaused){setPollPaused(false);setBusy(true);setPollAttempt(value=>value+1);}}catch(e){setError(String(e));}}
 function replaySaved(){rememberAnalysis(null);setPendingUpload(null);setRestored(false);setPollPaused(false);setBusy(false);setIsReplay(true);setIds(null);setJob(null);setEvents([]);setError('');setResult(replay);setPhase('Review at a glance');setAnalyzedName(replay.source_title);setTimeout(()=>panel.current?.scrollIntoView({behavior:'auto',block:'start'}),50);}
 const availability=availabilityNotice(healthReason,healthReachable);
 return <><div className="input-actions"><span className="muted">{mode==='ab'?'Live screening accepts one video. Explore the recorded A/B experiment below.':'Whole-video review · up to 8 steps'}</span><span className="input-buttons"><button className="btn btn-primary btn-hero" disabled={!source||mode!=='single'||busy||pollPaused} onClick={()=>void start()}>{busy?'Analyzing…':pollPaused?'Updates paused':'Analyze video'}</button><a className="btn btn-quiet" href="#experiment">Explore a demo</a></span></div><button type="button" className="text-link replay-review" disabled={busy||pollPaused} onClick={replaySaved}>View an example review</button>{connected===false&&!busy&&<div className="analysis-availability"><div role="status"><p>{availability.message}</p><p>{availability.detail}</p></div><button type="button" className="btn btn-small" disabled={checkingAvailability} onClick={()=>void checkAvailability()}>{checkingAvailability?'Checking availability…':'Check availability'}</button></div>}
 {(phase||error||result)&&<div className={'live-analysis'+(busy?' is-running':'')+(result?' has-result':'')} ref={panel}><div className="live-analysis-head"><div><span className="eyebrow analysis-brand"><BrandMark/>{isReplay?'Recorded result · no new analysis':restored?'Restored review · no new analysis':busy?'Live engine · W&B Inference':'Screening result'}</span>{(busy||!result)&&<h2>{phase}</h2>}{analyzedName&&<p className="live-source-name">{analyzedName}</p>}</div>{job&&<span className="mono">{Number.isFinite(job.elapsed_ms)?(job.elapsed_ms/1000).toFixed(1)+'s':'In progress'}</span>}</div><p className="live-note">AI suggestions. Review before editing.</p>{recoveryUnavailable&&(ids||pendingUpload)&&<p className="live-note">This browser could not save refresh recovery. Keep this tab open for this review.</p>}{error&&<div className="callout callout-bad" role="alert">{error}</div>}<div className="live-status" role="status" aria-live="polite">{busy?phase:result?.status==='needs_review'?'Some evidence needs review.':job?.error||''}</div>{pollPaused&&<button type="button" className="btn btn-small" onClick={checkProgress}>Check progress</button>}{pollPaused&&pendingUpload&&<><p className="live-note">You can also choose the same file again and retry its original request.</p><button type="button" className="btn btn-small" disabled={!source||mode!=='single'||busy} onClick={()=>void start(pendingUpload)}>Retry same upload</button></>}{(busy||pollPaused)&&ids&&<button className="btn btn-small" onClick={()=>void cancel()}>Stop analysis</button>}
 {events.length>0&&<details className="live-progress" open={busy}><summary>Actual workflow progress · {events.length} events</summary><ol>{events.map(e=><li key={e.seq}><span className="mono">{String(e.seq).padStart(2,'0')}</span><span>{e.message||e.stage.replaceAll('_',' ').toLowerCase()}</span></li>)}</ol></details>}
 {result&&<><ScoreSummary scorecard={result.scorecard} reportStatus={result.status}/><RiskChart windows={result.windows} duration={result.duration_ms}/><details className="complete-evidence"><summary>Evidence details and Weave trace</summary>{isReplay&&<p className="live-note">The example uses short editorial summaries of saved model suggestions. The original wording is preserved below.</p>}<div className="live-result-meta"><span>{result.model_calls} model calls</span><span>{result.duration_ms>0?(result.duration_ms/1000).toFixed(1)+'s video':'Preparing media'}</span>{trace(result.weave_url)&&<a href={trace(result.weave_url)} target="_blank" rel="noreferrer">Open live Weave trace ↗</a>}</div><div className="live-evidence-list">{result.windows.map((w,index)=><details className="live-evidence-row" key={w.end_ms}>
 <summary><span className="evidence-section">{index===0?'Opening':index===result.windows.length-1?'Ending':'Part '+(index+1)} <b>{sectionRange(w)}</b></span><span className="evidence-finding">{findingForMoment(w).text}</span><span className="evidence-risk">{riskLabel(w)}</span></summary>
 <div className="evidence-body">{w.validation_issues.length>0&&<><p className="citation-warning">Evidence checks need review:</p><ul>{w.validation_issues.map((v,i)=><li key={i}>{v}</li>)}</ul></>}
 {w.judgment&&<details className="screening-details"><summary>Original model response</summary><p className="live-note">Original wording, including unverified claims. The risk shown above applies the evidence checks.</p><p>{w.judgment.understanding}</p><ul>{w.judgment.observations.map((o,i)=><li key={i}><span className="observation-kind">{o.kind.replaceAll('_',' ')}</span><p>{o.text}</p><small>{o.frame_timestamps_ms.length>0?'Frames: '+o.frame_timestamps_ms.map(t=>(t/1000).toFixed(2)+'s').join(', '):''}{o.asr_quote?'Transcript: “'+o.asr_quote+'”':''}</small></li>)}</ul><p className="review-suggestion">{w.judgment.suggestion}</p>{w.judgment.uncertainties.map((u,i)=><p className="live-note" key={i}>{u}</p>)}</details>}
 {trace(w.weave_url)&&<a className="text-link" href={trace(w.weave_url)} target="_blank" rel="noreferrer">Inspect this step ↗</a>}</div>
 </details>)}</div></details></>}
 </div>}</>;
}
