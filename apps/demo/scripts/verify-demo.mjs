import fs from 'node:fs';import crypto from 'node:crypto';import assert from 'node:assert/strict';
const d=JSON.parse(fs.readFileSync(new URL('../public/evidence.json',import.meta.url)));
assert.equal(d.final_version,'A');assert.equal(d.experiment.decision,'reject_keep_inputs');assert.equal(d.workflow_stages.length,60);assert.equal(d.usage.model_calls,39);
for(const v of d.versions){assert.equal(crypto.createHash('sha256').update(fs.readFileSync(new URL('../public'+v.src,import.meta.url))).digest('hex'),v.sha256);}
const ids=new Set(d.workflow_stages.map(s=>s.id));for(const s of d.workflow_stages){assert.ok(!s.parent_id||ids.has(s.parent_id));assert.ok(s.weave_url.startsWith('https://wandb.ai/'));}
for(const f of ['../public/evidence.json','../src/App.tsx','../src/Landing.tsx']){const text=fs.readFileSync(new URL(f,import.meta.url),'utf8');assert.ok(!/\/Users\/|api[_-]?key|Bearer |sk-proj-|wandb_v1_|127\.0\.0\.1/.test(text));}
const app=fs.readFileSync(new URL('../src/App.tsx',import.meta.url),'utf8');assert.ok(!/fetch\(|XMLHttpRequest|\/api\//.test(app));console.log('Public demo verified: 3 exact media hashes; 60 connected stages; retained A; curated records and no private paths.');

const landing=fs.readFileSync(new URL("../src/Landing.tsx",import.meta.url),"utf8");assert.ok(landing.includes("Find what loses the viewer."));assert.ok(landing.includes("URL.createObjectURL"));assert.ok(landing.includes("URL.revokeObjectURL"));assert.ok(landing.includes('LiveAnalysis'));

const live=fs.readFileSync(new URL('../src/LiveAnalysis.tsx',import.meta.url),'utf8');assert.ok(!/localStorage|sessionStorage|sk-proj-|wandb_v1_|apikey_/.test(live));assert.ok(live.includes('AI suggestions. Review before editing.'));assert.ok(live.includes('crypto.randomUUID()'));

const screening=JSON.parse(fs.readFileSync(new URL('../src/screening-replay.json',import.meta.url)));assert.equal(screening.display_mode,'recorded');assert.equal(screening.model_calls,3);assert.equal(screening.semantic_grounding_verified,false);assert.equal(screening.automatic_edit_allowed,false);assert.ok(screening.weave_url.startsWith('https://wandb.ai/'));
