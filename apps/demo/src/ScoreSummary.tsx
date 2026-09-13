import {useId} from 'react';
import {aspectLabel, sectionTime} from './review-state';
import {metricPresentation, scoreSummaryState, supportedImprovements, SCORE_LABELS, type Scorecard} from './score-summary';

export default function ScoreSummary({scorecard, reportStatus}: {scorecard?: Scorecard | null; reportStatus: string}) {
  const headingId = useId();
  const state = scoreSummaryState(scorecard, reportStatus);
  if (state.state !== 'ready' || !scorecard) {
    return <p className="score-summary-note">{state.message}</p>;
  }
  const improvements = supportedImprovements(scorecard.improvements);
  return <section className="score-summary" aria-labelledby={headingId}>
    <div className="score-summary-heading"><h2 id={headingId}>Your video at a glance</h2><p>{state.message}</p></div>
    <div className="score-metrics">
      {(Object.keys(SCORE_LABELS) as Array<keyof typeof SCORE_LABELS>).map(key => {
        const supplied = scorecard.metrics[key];
        const metric = metricPresentation(scorecard.status === 'unavailable' ? {...supplied, score: null} : supplied, scorecard.status !== 'complete');
        return <article className={'score-metric' + (metric.score === null ? ' is-unrated' : '')} key={key}>
          <h3>{SCORE_LABELS[key]}</h3>
          <p className="score-value" aria-label={metric.score === null ? 'Not rated' : `${metric.value} out of 100`}>
            {metric.value}{metric.score !== null && <span aria-hidden="true">/100</span>}
          </p>
          <div className="score-track" aria-hidden="true"><span style={{width: `${metric.score ?? 0}%`}} /></div>
          <p className={'score-state' + (metric.provisional ? ' is-provisional' : '')}>{metric.status}</p>
          <p className="score-coverage">{metric.sections}{metric.duration && <span>{metric.duration}</span>}</p>
        </article>;
      })}
    </div>
    <div className="score-improvements"><h3>What to improve first</h3>
      {improvements.length ? <ol>{improvements.map(item => <li key={`${item.window_index}:${item.aspect}:${item.action}`}>
        <span className="score-improvement-time">{sectionTime(item.start_ms)}–{sectionTime(item.end_ms)}</span>
        <div><h4>{item.action}</h4><p>{item.reason}</p>
          <details className="score-improvement-evidence"><summary>Evidence</summary><p>{aspectLabel(item.aspect)} · Section {item.window_index + 1} · Observation{item.observation_indices.length === 1 ? '' : 's'} {[...new Set(item.observation_indices)].map(index => index + 1).join(', ')}</p><p>Inspect the matching section in Evidence details below.</p></details>
        </div>
      </li>)}</ol> : <p className="score-empty">{scorecard.status === 'complete' ? 'No supported change was identified.' : 'More evidence is needed before recommending a change.'}</p>}
    </div>
    <details className="score-method"><summary>How these scores work</summary>
      <p>{scorecard.method}</p>
      <dl>{(Object.keys(SCORE_LABELS) as Array<keyof typeof SCORE_LABELS>).map(key => <div key={key}><dt>{SCORE_LABELS[key]}</dt><dd>{scorecard.metrics[key].reason}</dd></div>)}</dl>
      <ul>{scorecard.limitations.map((limitation, index) => <li key={index}>{limitation}</li>)}</ul>
    </details>
  </section>;
}
