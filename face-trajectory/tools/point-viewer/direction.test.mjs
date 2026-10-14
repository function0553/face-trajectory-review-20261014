import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {prepareTrace,connectionGroups,connectionDirections} from './trace.mjs';

const input=JSON.parse(fs.readFileSync(new URL('./sample.json',import.meta.url),'utf8'));
const pair=connectionGroups(prepareTrace(input).drawable)[1];
test('uncertain forward and return records retain both nominal directions',()=>{
  const d=connectionDirections(pair);
  assert.equal(d.forward,'UNCERTAIN_CLOCK_ORDER');assert.equal(d.reverse,'UNCERTAIN_CLOCK_ORDER');
});
test('selecting record three keeps C-to-B only, without asserting confirmed time order',()=>{
  const d=connectionDirections(pair,2);
  assert.equal(d.forward,null);assert.equal(d.reverse,'UNCERTAIN_CLOCK_ORDER');
  assert.equal(d.selected.from_camera_id,'C');assert.equal(d.selected.to_camera_id,'B');
});
test('confirmed and uncertain directions have independent certainty',()=>{
  const mixed={...pair,records:pair.records.map((r,i)=>({...r,order_status:i?'UNCERTAIN_CLOCK_ORDER':'ORDERED'}))};
  const d=connectionDirections(mixed);
  assert.equal(d.forward,'ORDERED');assert.equal(d.reverse,'UNCERTAIN_CLOCK_ORDER');
});
test('individually selecting a confirmed direction does not inherit another record uncertainty',()=>{
  const known={...pair,records:pair.records.map(r=>({...r,order_status:'ORDERED'}))};
  const d=connectionDirections(known,1);
  assert.equal(d.forward,'ORDERED');assert.equal(d.reverse,null);
});
