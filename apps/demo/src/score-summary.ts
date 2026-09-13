export type ScoreMetric = {
  score: number | null;
  coverage: number;
  rated_ms: number;
  total_ms: number;
  rated_sections: number;
  total_sections: number;
  provisional: boolean;
  reason: string;
};

export type ScoreImprovement = {
  window_index: number;
  start_ms: number;
  end_ms: number;
  aspect: string;
  action: string;
  reason: string;
  observation_indices: number[];
};

export type Scorecard = {
  version: 'creative-potential-v1';
  status: 'complete' | 'partial' | 'unavailable';
  evidence_level: 'model_rubric';
  predicts_audience_outcomes: false;
  metrics: {creative: ScoreMetric; retention: ScoreMetric; virality: ScoreMetric};
  improvements: ScoreImprovement[];
  method: string;
  limitations: string[];
};

export const SCORE_DISCLAIMER = 'AI rubric · not audience predictions';
export const SCORE_LABELS = {
  creative: 'Creative score',
  retention: 'Retention potential',
  virality: 'Virality potential',
} as const;

/** Format backend ratings only. Missing or malformed evidence never becomes zero. */
export function metricPresentation(metric: ScoreMetric, partial = false) {
  const validCoverage = Number.isFinite(metric.coverage) && metric.coverage > 0 && metric.coverage <= 1;
  const validTime = Number.isFinite(metric.rated_ms) && Number.isFinite(metric.total_ms) &&
    metric.rated_ms > 0 && metric.total_ms >= metric.rated_ms;
  const validSections = Number.isInteger(metric.rated_sections) && Number.isInteger(metric.total_sections) &&
    metric.rated_sections > 0 && metric.total_sections >= metric.rated_sections;
  const score = typeof metric.score === 'number' && Number.isFinite(metric.score) &&
    metric.score >= 0 && metric.score <= 100 && validCoverage && validTime && validSections ? metric.score : null;
  const provisional = score !== null && (partial || metric.provisional || metric.coverage < 1 ||
    metric.rated_sections < metric.total_sections || metric.rated_ms < metric.total_ms);
  return {
    score,
    value: score === null ? 'Not rated' : String(Math.round(score)),
    status: score === null ? 'Insufficient evidence' : provisional ? 'Provisional' : 'Rated',
    provisional,
    sections: validSections ? `${metric.rated_sections} of ${metric.total_sections} sections rated` : 'No sections rated',
    duration: validTime ? `${(metric.rated_ms / 1000).toFixed(1)} / ${(metric.total_ms / 1000).toFixed(1)}s of evidence` : null,
    reason: metric.reason,
  };
}

/** Preserve complete actions and their qualifications; never rewrite questions as assertions. */
export function supportedImprovements(improvements: ScoreImprovement[]): ScoreImprovement[] {
  const seen = new Set<string>();
  return improvements.filter(item => {
    if (!item.action?.trim() || !item.reason?.trim() || /[?？؟]|\.\.\.|…/.test(item.action) ||
      /^(?:does|do|did|is|are|was|were|can|could|would|should|will|has|have)\b/i.test(item.action.trim()) ||
      !Number.isFinite(item.start_ms) || !Number.isFinite(item.end_ms) || item.start_ms < 0 || item.end_ms <= item.start_ms ||
      !Number.isInteger(item.window_index) || item.window_index < 0 || !Array.isArray(item.observation_indices) ||
      !item.observation_indices.length || item.observation_indices.some(index => !Number.isInteger(index) || index < 0)) return false;
    const key = `${item.window_index}:${item.aspect}:${item.action.trim()}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).slice(0, 3);
}

export function scoreSummaryState(scorecard: Scorecard | null | undefined, reportStatus: string) {
  if (!['complete', 'needs_review'].includes(reportStatus)) {
    return {state: 'pending' as const, message: ['pending', 'running', 'queued'].includes(reportStatus)
      ? 'Scores arrive when the review finishes.' : 'Scores are unavailable for this incomplete review.'};
  }
  if (!scorecard) return {state: 'legacy' as const, message: 'Scores appear on new analyses.'};
  if (scorecard.version !== 'creative-potential-v1' || scorecard.evidence_level !== 'model_rubric' ||
    scorecard.predicts_audience_outcomes !== false) {
    return {state: 'unavailable' as const, message: 'Scores are unavailable for this review.'};
  }
  return {state: 'ready' as const, message: SCORE_DISCLAIMER};
}
