import test from 'node:test';
import assert from 'node:assert/strict';
import {prepareTrace,projection} from './trace.mjs';

function fixture() {
  const make=(source,id,camera,xyz)=>({source_id:source,observation_id:id,camera_id:camera,status:'OBSERVED',
    captured_at_unix_ms:1000,clock_uncertainty_ms:0,position_status:'BOUND_MARKER',position_candidates:[{local_xyz:xyz}]});
  return {ok:true,activity_id:'A',point_trace:{target_id:'PERSON',version:1,coordinate_frame:'TEST:local',
    drawing_mode:'CAMERA_POINT_CONNECTIONS',observations:[make('S1','same','A',[0,0,0]),make('S2','same','B',[10,0,10])],
    connections:[{from_source_id:'S1',to_source_id:'S2',from_observation_id:'same',to_observation_id:'same',
      from_camera_id:'A',to_camera_id:'B',order_status:'ORDERED',drawing_status:'DRAWABLE',coordinates:[[0,0,0],[10,0,10]],
      physical_path_observed:false,meaning:'DIRECT_CAMERA_POINT_CONNECTION'}]}};
}
test('different sources with equal original IDs remain distinct',()=>assert.equal(prepareTrace(fixture()).drawable.length,1));
test('unresolved positions cannot carry a manufactured line',()=>{const f=fixture();f.point_trace.connections[0].drawing_status='UNRESOLVED_POSITION';assert.throws(()=>prepareTrace(f));});
test('endpoint coordinates must match evidence',()=>{const f=fixture();f.point_trace.connections[0].coordinates[1][0]=11;assert.throws(()=>prepareTrace(f));});
test('duplicate event keys and unknown endpoints rejected',()=>{const f=fixture();f.point_trace.observations.push({...f.point_trace.observations[0]});assert.throws(()=>prepareTrace(f));const g=fixture();g.point_trace.connections[0].to_source_id='OTHER';assert.throws(()=>prepareTrace(g));});
test('error replies and legacy contracts rejected',()=>{assert.throws(()=>prepareTrace({ok:false}));const f=fixture();delete f.point_trace.connections;assert.throws(()=>prepareTrace(f));});
test('clock uncertainty and historical freshness preserved',()=>{const f=fixture();f.point_trace.connections[0].order_status='UNCERTAIN_CLOCK_ORDER';f.point_trace.freshness={status:'STALE_ASSOCIATION'};const v=prepareTrace(f);assert.equal(v.lines[0].order_status,'UNCERTAIN_CLOCK_ORDER');assert.equal(v.freshness,'STALE_ASSOCIATION');});
test('nonfinite coordinates and unsafe clock values rejected',()=>{const f=fixture();f.point_trace.observations[0].position_candidates[0].local_xyz[0]=Infinity;assert.throws(()=>prepareTrace(f));const g=fixture();g.point_trace.observations[0].captured_at_unix_ms=Number.MAX_SAFE_INTEGER;assert.throws(()=>prepareTrace(g));});
test('projection keeps ordinary and extreme finite coordinates finite',()=>{for(const ps of [[[0,0,0],[10,0,10]],[[-1e308,0,0],[1e308,0,1e308]]]){const p=projection(ps,[0,2]);for(const value of ps)assert.ok(p(value).every(Number.isFinite));}});
