import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createLocalPreview} from '../src/local-preview.ts';

test('missing or throwing preview APIs cannot prevent the upload selection',()=>{
 for(const api of [{},{createObjectURL:()=>{throw new Error('blocked')}}]){
  const result=createLocalPreview(new Blob(['video']),api);
  assert.equal(result.url,null);assert.doesNotThrow(()=>result.release());
 }
});
test('a valid preview is released once and cleanup failures stay isolated',()=>{
 let released=0;
 const result=createLocalPreview(new Blob(['video']),{createObjectURL:()=> 'blob:fixture',revokeObjectURL:()=>{released++;throw new Error('blocked cleanup')}});
 assert.equal(result.url,'blob:fixture');result.release();result.release();assert.equal(released,1);
});
