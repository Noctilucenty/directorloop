export const REVIEW_RECOVERY_KEY = 'directorloop.review.v1';
export type SavedAnalysis = {job_id: string; screen_id: string; name: string};
export type PendingUpload = {request_id: string; name: string};
export type ReviewRecovery = {version: 1; session: string; analysis: SavedAnalysis | null; pending_request?: PendingUpload};
export type RecoveryStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
const identifier = (value: unknown, prefix: 'job' | 'screen'): value is string => typeof value === 'string' && value.length <= 80 && new RegExp(`^${prefix}_[a-f0-9]+_[a-f0-9]+$`).test(value);

/** Only a same-tab capability and submitted job identity survive refresh. */
export function parseReviewRecovery(value: string | null): ReviewRecovery | null {
  if (!value || value.length > 4096) return null;
  try {
    const parsed = JSON.parse(value);
    if (parsed?.version !== 1 || typeof parsed.session !== 'string' || !/^[a-f0-9]{32}$/.test(parsed.session)) return null;
    if (parsed.analysis === null) {
      if (parsed.pending_request === undefined) return {version: 1, session: parsed.session, analysis: null};
      const pending = parsed.pending_request;
      if (!pending || typeof pending.request_id !== 'string' || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(pending.request_id) ||
        typeof pending.name !== 'string' || !pending.name.trim() || pending.name.length > 512) return null;
      return {version: 1, session: parsed.session, analysis: null, pending_request: {request_id: pending.request_id, name: pending.name}};
    }
    const analysis = parsed.analysis;
    if (!analysis || !identifier(analysis.job_id, 'job') || !identifier(analysis.screen_id, 'screen') ||
      analysis.job_id.slice(4) !== analysis.screen_id.slice(7) ||
      typeof analysis.name !== 'string' || !analysis.name.trim() || analysis.name.length > 512) return null;
    return {version: 1, session: parsed.session, analysis: {job_id: analysis.job_id, screen_id: analysis.screen_id, name: analysis.name}};
  } catch { return null; }
}

export function readReviewRecovery(storage: RecoveryStorage | null): ReviewRecovery | null {
  if (!storage) return null;
  try {
    const value = storage.getItem(REVIEW_RECOVERY_KEY);
    const record = parseReviewRecovery(value);
    if (value && !record) storage.removeItem(REVIEW_RECOVERY_KEY);
    return record;
  } catch { return null; }
}

export function writeReviewRecovery(storage: RecoveryStorage | null, record: ReviewRecovery): boolean {
  if (!storage) return false;
  try {
    const safe = parseReviewRecovery(JSON.stringify(record));
    if (!safe) return false;
    storage.setItem(REVIEW_RECOVERY_KEY, JSON.stringify(safe));
    return true;
  } catch {
    // Do not revive the previous video if its replacement could not be saved.
    try { storage.removeItem(REVIEW_RECOVERY_KEY); } catch { /* Storage is unavailable. */ }
    return false;
  }
}

export function reviewRecoveryStorage(): RecoveryStorage | null {
  try { return window.sessionStorage; } catch { return null; }
}

export function parseReviewRecoveryFragment(hash: string): ReviewRecovery | null {
  const match = /^#restore=([A-Za-z0-9_-]+)$/.exec(hash);
  if (!match || hash.length > 6000) return null;
  try {
    const encoded = match[1].replaceAll('-', '+').replaceAll('_', '/');
    const bytes = Uint8Array.from(atob(encoded + '='.repeat((4 - encoded.length % 4) % 4)), c => c.charCodeAt(0));
    const record = parseReviewRecovery(new TextDecoder('utf-8', {fatal: true}).decode(bytes));
    return record?.analysis ? record : null;
  } catch { return null; }
}

/** Consume only a validated existing-job capability; no URL or executable input is accepted. */
export function consumeReviewRecoveryFragment(
  location: {hash: string; pathname: string; search: string},
  replaceUrl: (url: string) => void,
  storage: RecoveryStorage | null,
): ReviewRecovery | null {
  if (location.hash !== '#restore' && !location.hash.startsWith('#restore=')) return null;
  const record = parseReviewRecoveryFragment(location.hash);
  try { replaceUrl(location.pathname + location.search + '#'); } catch { return null; }
  if (record) writeReviewRecovery(storage, record);
  return record;
}

export function initialReviewRecovery(): ReviewRecovery | null {
  const storage = reviewRecoveryStorage();
  try {
    const imported = consumeReviewRecoveryFragment(window.location, url => window.history.replaceState(window.history.state, '', url), storage);
    if (imported) return imported;
  } catch { /* Continue with the same-tab record if the browser blocks URL access. */ }
  return readReviewRecovery(storage);
}

export type UploadRecoveryResponse = {state?: string; job_id?: string; screen_id?: string; error?: string};
export function uploadRecoveryDecision(response: UploadRecoveryResponse | null, checks: number, failures: number) {
  if (response?.state === 'submitted' && identifier(response.job_id, 'job') && identifier(response.screen_id, 'screen') && response.job_id.slice(4) === response.screen_id.slice(7)) {
    return {kind: 'submitted' as const, ids: {job_id: response.job_id, screen_id: response.screen_id}, message: 'Upload recovered. Retrieving your review.'};
  }
  if (response?.state === 'failed') return {kind: 'failed' as const, message: 'The original upload could not be completed. You can choose a video and try again.'};
  if (checks >= 30 || failures >= 5) return {kind: 'paused' as const, message: 'Upload confirmation is unavailable. Check progress to look for the original upload; it will not be resubmitted.'};
  return {kind: 'waiting' as const, message: response?.state === 'pending' ? 'The original upload is still processing.' : 'Looking for confirmation of your original upload.'};
}
