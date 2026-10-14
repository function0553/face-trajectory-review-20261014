import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {prepareTrace,connectionGroups} from './trace.mjs';

const input=JSON.parse(fs.readFileSync(new URL('./sample.json',import.meta.url),'utf8'));
const records=prepareTrace(input).drawable;
test('actual sample keeps three records but draws two unique camera chords',()=>{
  assert.equal(records.length,3);
  const groups=connectionGroups(records);
  assert.equal(groups.length,2,'B-C and C-B must not be indistinguishable overlapping strokes');
  assert.deepEqual(groups.map(g=>g.records.map(r=>r.index)),[[0],[1,2]]);
});
test('minimal forward and return pair remains two independently selectable records',()=>{
  const pair=records.slice(1);
  const groups=connectionGroups(pair);
  assert.equal(groups.length,1);
  assert.deepEqual(groups[0].records.map(r=>[r.from_camera_id,r.to_camera_id]),[['B','C'],['C','B']]);
  assert.deepEqual(groups[0].coordinates,pair[0].coordinates);
});
test('distinct camera pairs are not merged merely because coordinates coincide',()=>{
  const a=records[0], other={...a,from_camera_id:'OTHER_CAMERA',index:100};
  assert.equal(connectionGroups([a,other]).length,2);
});
test('same camera pair with different actual endpoints retains separate geometry',()=>{
  const a=records[0], other={...a,coordinates:[[0,0,0],[16,0,8]],index:101};
  assert.equal(connectionGroups([a,other]).length,2);
});
