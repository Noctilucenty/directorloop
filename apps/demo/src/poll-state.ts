export type PollJob = {state:string;stage:string;error?:string|null};
export type PollReport = {status:string;error?:string|null};
export type PollEvent = {message:string;stage:string};
type Outcome<T> = PromiseSettledResult<T>;
const value = <T,>(result:Outcome<T>):T|undefined => result.status === 'fulfilled' ? result.value : undefined;
const code = (result:Outcome<unknown>):number|undefined => result.status === 'rejected' ? result.reason?.status : undefined;
export const finalReport = (status:string) => ['complete','needs_review','failed','canceled'].includes(status);

/** A saved result survives auxiliary status failures; retries only read existing work. */
export function decidePoll(jobResult:Outcome<PollJob>, eventResult:Outcome<PollEvent[]>, reportResult:Outcome<PollReport>, previousFailures:number) {
  const job=value(jobResult), report=value(reportResult), events=value(eventResult);
  const completed = report && finalReport(report.status);
  if (completed) return {stop:true,manual:false,failures:0,delay:0,phase:report.status==='failed'?'Review stopped':report.status==='canceled'?'Review canceled':'Review complete',error:report.error || (report.status==='failed'?'The review stopped before completion. Saved findings are shown below.':'')};
  if ([jobResult,eventResult,reportResult].some(result => [401,403].includes(code(result) ?? 0))) {
    return {stop:true,manual:false,failures:0,delay:0,phase:'Access to this review is unavailable',error:'This page can no longer access the review. Keep any saved results; automatic checks have stopped.'};
  }
  if (job && ['FAILED','CANCELED'].includes(job.state)) return {stop:true,manual:false,failures:0,delay:0,phase:job.state==='FAILED'?'Review stopped':'Review canceled',error:job.error || (job.state==='FAILED'?'The review stopped before completion. Saved findings are shown below.':'')};
  const pendingReport = job && !['COMPLETED','FAILED','CANCELED'].includes(job.state) && code(reportResult)===404;
  const healthy = Boolean(job && (report || pendingReport) && job.state!=='COMPLETED');
  if (healthy) return {stop:false,manual:false,failures:0,delay:2000,phase:events?.at(-1)?.message || (pendingReport?'Preparing the first review.':'Reviewing your video.'),error:''};
  const failures=previousFailures+1;
  const waitingForReport=job?.state==='COMPLETED';
  const manual=failures>=5;
  return {
    stop:manual,manual,failures,delay:Math.min(15000,2000*2**(failures-1)),
    phase:manual?'Updates paused':waitingForReport?'Finishing the saved report':'Reconnecting to progress',
    error:manual?'Live updates are unavailable. Check progress to retrieve the existing review; this will not start a new analysis.':waitingForReport?'The worker has finished. Waiting for its saved report.':'Progress updates were interrupted. Checking again shortly; the analysis is not resubmitted.',
  };
}
