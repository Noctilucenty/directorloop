import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {availabilityNotice} from '../src/availability-state.ts';

test('local request allowance does not masquerade as an offline engine or spent provider credits', () => {
  const result = availabilityNotice('Screening budget has insufficient headroom for 8 calls.', true);
  assert.equal(result.kind, 'allowance');
  assert.match(result.message, /request allowance/);
  assert.match(result.detail, /local usage limit/);
  assert.match(result.detail, /Provider credits are separate/);
  assert.ok(!/offline|out of credits/i.test(result.message));
});

test('network failure, missing reason, and actual provider quota stay distinct', () => {
  assert.equal(availabilityNotice(null, false).kind, 'connection');
  assert.equal(availabilityNotice('The analysis engine is offline.', true).kind, 'connection');
  assert.equal(availabilityNotice(null, true).kind, 'unavailable');
  assert.equal(availabilityNotice('Provider insufficient_quota', true).kind, 'provider');
  assert.equal(availabilityNotice('The live engine is not configured.', false).kind, 'configuration');
});

test('manual availability checks are bounded GET requests and cannot start or gate analysis', () => {
  const source = readFileSync(new URL('../src/LiveAnalysis.tsx', import.meta.url), 'utf8');
  const check = source.slice(source.indexOf('async function checkAvailability(){'), source.indexOf('useEffect(()=>{void checkAvailability()'));
  assert.ok(check.includes("method:'GET'"));
  assert.ok(check.includes('20000'));
  assert.ok(!/\/analyze|POST|setInterval|start\(/.test(check));
  assert.ok(source.includes('Check availability'));
  assert.ok(source.includes('setHealthReason(null);return body;'));
  assert.ok(source.includes("disabled={!source||mode!=='single'||busy||pollPaused}"));
  assert.ok(!/presenter/i.test(source));
});
