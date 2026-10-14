import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {prepareTrace,displayConnections,connectionGroups,connectionMarkers} from './trace.mjs';
const sample=JSON.parse(fs.readFileSync(new URL('./sample.json',import.meta.url),'utf8'));
function input() {
  const value=structuredClone(sample),c=value.point_trace.connections[0];
  c.geometry_kind='CAMERA_CHORD';c.route_status='AVAILABLE';c.route_geometry_kind='TOPOLOGY_INFERRED_PATH';
  c.route_waypoints=[{x:0,y:0,z:0},{x:7,y:0,z:0},{x:7,y:0,z:7},{x:15,y:0,z:7}];
  return value;
}
test('topology selection uses supplied bends while chord endpoints stay immutable',()=>{
  const value=input(),prepared=prepareTrace(value);
  assert.deepEqual(displayConnections(prepared,'CHORD')[0].display_coordinates,value.point_trace.connections[0].coordinates);
  assert.deepEqual(displayConnections(prepared,'TOPOLOGY')[0].display_coordinates,[[0,0,0],[7,0,0],[7,0,7],[15,0,7]]);
  assert.equal(displayConnections(prepared,'TOPOLOGY').length,1);
});
test('legacy traces without topology remain supported and never fabricate a path',()=>{
  const p=prepareTrace(sample);assert.equal(displayConnections(p,'CHORD').length,3);
  assert.equal(displayConnections(p,'TOPOLOGY').length,0);
});
test('one uncertain record always renders dashed two-way markers',()=>{
  const groups=connectionGroups(displayConnections(prepareTrace(sample)));
  const markers=connectionMarkers(groups[1],2);
  assert.equal(markers.start,'UNCERTAIN_CLOCK_ORDER');assert.equal(markers.end,'UNCERTAIN_CLOCK_ORDER');assert.equal(markers.uncertain,true);
  const group={...groups[0],records:groups[0].records.map(r=>({...r,order_status:'ORDERED'}))};
  const known=connectionMarkers(group);assert.equal(known.start,null);assert.equal(known.end,'ORDERED');assert.equal(known.uncertain,false);
});
test('same camera endpoints with different corridor bends are separate geometry',()=>{
  const first=displayConnections(prepareTrace(input()),'TOPOLOGY')[0];
  const second={...first,index:10,display_coordinates:[[0,0,0],[5,0,30],[15,0,7]]};
  assert.equal(connectionGroups([first,second]).length,2);
});
test('unreachable, HOLD and unresolved positions never produce a line',()=>{
  for (const status of ['HOLD','UNREACHABLE','UNRESOLVED_POSITION']) {
    const v=structuredClone(sample),c=v.point_trace.connections[0];c.drawing_status=status;c.coordinates=[];
    const p=prepareTrace(v);assert.equal(displayConnections(p).some(x=>x.index===0),false);
  }
  const v=input();v.point_trace.connections[0].route_status='UNREACHABLE';delete v.point_trace.connections[0].route_waypoints;
  assert.equal(displayConnections(prepareTrace(v)).some(x=>x.index===0),false);
});
test('invalid or claimed observed route geometry fails closed',()=>{
  for (const mutate of [c=>{c.route_waypoints[1].x=Infinity},c=>{c.route_geometry_kind='OBSERVED'},c=>{c.from_position=[999,0,0]},c=>{c.direction_status='CONFIRMED'}]) {
    const v=input();mutate(v.point_trace.connections[0]);assert.throws(()=>prepareTrace(v));
  }
});
