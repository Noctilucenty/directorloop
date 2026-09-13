/** Demo usage limits, provider billing, and connectivity are separate states. */
export function availabilityNotice(reason: string | null, reachable: boolean | null) {
  if (/screening budget|insufficient headroom|demo (?:request |analysis )?allowance/i.test(reason ?? '')) {
    return {kind: 'allowance', message: 'The demo’s request allowance cannot cover another full review.', detail: 'This is the demo’s local usage limit. Provider credits are separate.'};
  }
  if (/not configured/i.test(reason ?? '')) {
    return {kind: 'configuration', message: 'Live analysis is not connected yet.', detail: 'You can still explore the recorded review.'};
  }
  if (/insufficient_quota|provider.*(?:credits|billing|quota)|billing.*(?:limit|disabled)/i.test(reason ?? '')) {
    return {kind: 'provider', message: 'The model provider is reporting a billing or quota limit.', detail: 'Check availability after the provider account is updated.'};
  }
  if (reachable === false || /offline|unreachable|connection|timed? out/i.test(reason ?? '')) {
    return {kind: 'connection', message: 'The analysis engine could not be reached.', detail: 'You can still explore the recorded review.'};
  }
  return {kind: 'unavailable', message: 'Live analysis is temporarily unavailable.', detail: 'Check availability to get the latest status.'};
}
