"""Real signed Go/C++ point queries with fresh synthetic state only."""
import argparse
import base64
import copy
import hashlib
import json
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'evidence/2026-10-10-activity-retention'))
from verify_core import T0, SPACE, POLICY, vec, iso, scope, profile

def site():
    value = dict(scene_version='SYNTHETIC:points', model_id='SYNTHETIC:model', scene_url='SYNTHETIC:unused',
        tileset_id='SYNTHETIC:unused', calibration_profile='SYNTHETIC:calibration', coordinate_frame='SYNTHETIC:local',
        world_coordinate_frame='ECEF_WGS84', unit='m', source_evidence_id='SYNTHETIC:independent-markers',
        duplicate_tolerance_m=0.1, to_world_matrix=[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1], cameras=[], annotations=[])
    for i, camera in enumerate(['A', 'B', 'C', 'CONFLICT', 'MISSING']):
        value['cameras'].append(dict(camera_id=camera, vcl_channel_id=None))
        if camera == 'MISSING':
            continue
        value['annotations'].append(dict(annotation_id='point:'+camera, source_scene_id='SYNTHETIC:source', name=camera,
            kind_id='SYNTHETIC:camera', role='CAMERA', shape='point', visible=True, camera_id=camera, points=[[i*15,0,i*7]]))
    other = copy.deepcopy(value['annotations'][-1]); other['annotation_id']='conflict:other'; other['points']=[[90,0,20]]
    value['annotations'].append(other)
    return value

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--worker',type=Path,required=True); parser.add_argument('--service',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True); parser.add_argument('--example',type=Path)
    args=parser.parse_args()
    if args.output.exists() or (args.example and args.example.exists()):
        raise RuntimeError('Fresh output and example paths required')
    args.output.mkdir(parents=True)
    key=Ed25519PrivateKey.generate()
    keys=args.output/'public-keys.json'; proof=args.output/'clock-proof.txt'; db=args.output/'isolated.sqlite'
    keys.write_text(json.dumps({'keys':{'synthetic':base64.b64encode(key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()}}))
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    url='http://127.0.0.1:'+str(port); calls=[]; checks=[]; proc=None; failure=None; sample=None
    def signed(body):
        data=json.dumps(body,separators=(',',':')).encode()
        enc=lambda value:base64.urlsafe_b64encode(value).decode().rstrip('=')
        return enc(data)+'.'+enc(key.sign(data))
    def token(activity):
        return signed(dict(issuer='SYNTHETIC',key_id='synthetic',subject='viewer-'+activity,session='test-'+activity,
            context_id='viewer-'+activity,context_generation=1,authz_revision=1,not_before_unix_ms=T0-100000,
            expires_unix_ms=T0+3600000,grants=[dict(activity_id=activity,actions=['query','config','monitor','ingest','identity'])],
            global_actions=['global_config']))
    def call(path,body=None,bearer=None,expected=200,record=True):
        request=urllib.request.Request(url+path,data=None if body is None else json.dumps(body).encode(),
            headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+bearer} if bearer else {})})
        try:
            with urllib.request.urlopen(request,timeout=15) as response:
                status=response.status; result=json.load(response)
        except urllib.error.HTTPError as error:
            status=error.code;result=json.load(error)
        if record:calls.append(dict(path=path,request=body,status=status,response=result))
        if status!=expected:raise AssertionError((path,status,expected,result))
        return result
    def check(name,condition):
        checks.append(dict(name=name,passed=bool(condition)))
        if not condition:raise AssertionError(name)
    def start():
        log=(args.output/('service-'+str(len(checks))+'.log')).open('w')
        process=subprocess.Popen([str(args.service.resolve()),'-worker',str(args.worker.resolve()),'-state-file',str(db.resolve()),
            '-addr','127.0.0.1:'+str(port),'-auth-public-key-file',str(keys.resolve()),'-auth-issuer','SYNTHETIC',
            '-clock-public-key-file',str(keys.resolve()),'-clock-issuer','SYNTHETIC','-clock-proof-file',str(proof.resolve())],stdout=log,stderr=log)
        log.close()
        for _ in range(100):
            try:health=call('/health',record=False);break
            except (OSError,urllib.error.URLError):time.sleep(.05)
        else:
            process.terminate();process.wait(15);raise RuntimeError('Test service unavailable')
        now=T0+300000
        if db.exists():
            with sqlite3.connect(db) as connection:
                watermark=connection.execute("SELECT value FROM state WHERE ns='retention_meta' AND key='utc_watermark'").fetchone()
                if watermark:now=max(now,int(watermark[0])+1000)
        proof.write_text(signed(dict(issuer='SYNTHETIC',key_id='synthetic',challenge=health['clock']['challenge'],now_unix_ms=now,
            uncertainty_ms=0,valid_for_ms=120000,source_verified=True,source='SYNTHETIC_TEST',evidence_id='SYNTHETIC:point-test')))
        return process
    def stop():
        proc.terminate();proc.wait(15)
    graph=dict(version='SYNTHETIC:graph',edges=[])
    def observe(event,camera,t,source='S1',activity='A',clock_name='good'):
        return dict(scope(activity),route_rule_id='ROUTE-'+activity,time_record=dict(profile_version=clock_name,raw_time=iso(T0+t)),
            observation=dict(observation_id=event,source_id=source,camera_id=camera,frame_id=camera+':'+event,
                feature_space=SPACE,embedding=vec(0),detection_score=.99,face_size_px=64),policy=POLICY,graph=graph)
    def query(activity='A',version=None,expected=200):
        request=dict(scope(activity),target_id='PERSON')
        if version is not None:request['version']=version
        return call('/v1/point-trajectories/get',request,token(activity),expected)
    try:
        proc=start();A=token('A');B=token('B')
        call('/v1/site-scenes',dict(site=site()),A)
        call('/v1/point-graphs',dict(point_graph=dict(graph_version=graph['version'],scene_version='SYNTHETIC:points',
            source_evidence_id='SYNTHETIC:points-only',coordinate_frame='SYNTHETIC:local',topology_verified=False,topology_is_complete=False,edges=[])),A)
        for camera in ['A','B','C','CONFLICT','MISSING']:
            for name,error,verified in [('good',0,True),('overlap',40000,True),('held',0,False)]:
                value=profile(name,error=error,verified=verified)['profile'];value['camera_id']=camera
                call('/v1/clocks/profiles',dict(profile=value),A)
        for activity,bearer in [('A',A),('B',B)]:
            call('/v1/activities/sync',dict(activity_id=activity,activity_num='NUM-'+activity,venue_id='SYNTHETIC:VENUE',config_version=1,expected_config_version=0),bearer)
            bindings=[dict(source_id=source,device_id=camera,kind='CAMERA',route_rule_id='ROUTE-'+activity)
                for source in ['S1','S2'] for camera in ['A','B','C','CONFLICT','MISSING']]
            call('/v1/activities/bindings',dict(activity_id=activity,config_version=1,binding_version=1,expected_binding_version=0,bindings=bindings),bearer)
            call('/v1/activities/monitoring',dict(activity_id=activity,config_version=1,binding_version=1,monitoring_version=1,expected_monitoring_version=0,enabled=True),bearer)
            call('/v1/monitoring/persons',dict(scope(activity),person_id='PERSON',category='NORMAL',revision=1,expected_revision=0,
                evidence_id='SYNTHETIC:independent-enrollment',templates=[dict(template_id='REGISTERED',feature_space=SPACE,embedding=vec(0))]),bearer)
        check('query_requires_signed_identity',call('/v1/point-trajectories/get',dict(scope(),target_id='PERSON'),expected=401)['ok'] is False)
        check('other_activity_query_denied',call('/v1/point-trajectories/get',dict(scope('B'),target_id='PERSON'),A,403)['ok'] is False)
        call('/v1/monitoring/observe',observe('shared','A',0),A)
        check('one_confirmed_point_no_line',query()['point_trace']['connections']==[])
        repeat=observe('repeat','A',10000);call('/v1/monitoring/observe',repeat,A)
        check('same_camera_repeat_no_self_loop',query()['point_trace']['connections']==[])
        call('/v1/monitoring/observe',observe('shared','B',30000,source='S2'),A)
        trace=query()['point_trace'];line=trace['connections'][0]
        check('two_point_direct_line',line['drawing_status']=='DRAWABLE' and len(line['coordinates'])==2 and line['order_status']=='ORDERED')
        check('cross_source_ids_are_original',line['from_source_id']=='S1' and line['from_observation_id']=='repeat' and line['to_source_id']=='S2' and line['to_observation_id']=='shared')
        call('/v1/monitoring/observe',observe('c','C',60000,clock_name='overlap'),A)
        trace=query()['point_trace']
        check('uncertain_clock_line_retained',trace['connections'][1]['order_status']=='UNCERTAIN_CLOCK_ORDER')
        call('/v1/monitoring/observe',observe('return','B',90000),A)
        call('/v1/monitoring/observe',observe('conflict','CONFLICT',120000),A)
        call('/v1/monitoring/observe',observe('missing','MISSING',150000),A)
        check('held_event_stays_out',call('/v1/monitoring/observe',observe('held','A',160000,clock_name='held'),A)['status']=='HOLD')
        late=observe('late','A',20000);call('/v1/monitoring/observe',late,A)
        before=query();trace=before['point_trace']
        check('late_event_updates_boundary',trace['connections'][0]['from_observation_id']=='late')
        check('unresolved_positions_not_drawn',sum(c['drawing_status']=='UNRESOLVED_POSITION' and not c['coordinates'] for c in trace['connections'])==2)
        check('no_inferred_route_or_person_coordinates',all(not c['physical_path_observed'] and c['meaning']=='DIRECT_CAMERA_POINT_CONNECTION' for c in trace['connections']) and trace['detailed_walk_path_observed'] is False)
        check('only_confirmed_events_in_trace',len(trace['observations'])==8 and all(o['observation_id']!='held' for o in trace['observations']))
        call('/v1/monitoring/observe',observe('shared','C',40000,activity='B'),B)
        check('same_person_two_activities_isolated',len(query('B')['point_trace']['observations'])==1 and not query('B')['point_trace']['connections'])
        check('retry_keeps_version',call('/v1/monitoring/observe',late,A)['duplicate'] and query()['point_trace']['version']==trace['version'])
        def state_rows():
            with sqlite3.connect(db) as connection:return connection.execute('SELECT ns,key,value,ref,track,meta FROM state ORDER BY ns,key').fetchall()
        rows=state_rows();query();after_rows=state_rows()
        business=lambda records:[row for row in records if row[:2] != ('retention_meta','utc_watermark')]
        check('view_query_keeps_identity_trajectory_and_notifications',business(after_rows)==business(rows))
        (args.output/'query-state-audit.json').write_text(json.dumps(dict(
            allowed_update='Existing monotonic trusted UTC watermark only',business_rows_unchanged=True,
            watermark_before=[row for row in rows if row[:2]==('retention_meta','utc_watermark')],
            watermark_after=[row for row in after_rows if row[:2]==('retention_meta','utc_watermark')]),indent=2),encoding='utf-8')
        sample=copy.deepcopy(before);sample['evidence_kind']='SYNTHETIC_INTERFACE_EXAMPLE'
        stop();proc=start()
        check('restart_preserves_point_connections',query()['point_trace']==trace)
        call('/v1/associations/correct',dict(scope(),source_id='S2',observation_id='shared',target_id='OTHER',evidence_id='SYNTHETIC:independent-correction'),A)
        check('correction_invalidates_current_trace',query(expected=409)['error_code']=='REBUILD_REQUIRED')
        historical=query(version=trace['version'])
        check('historical_trace_marks_changed_attribution',historical['point_trace']['freshness']['status']=='STALE_ASSOCIATION')
        call('/v1/associations/rebuild',dict(scope(),target_id='PERSON',expected_version=trace['version'],graph=graph,
            options=dict(max_clock_uncertainty_ms=60000),reason='SYNTHETIC:rebuild-after-correction'),A)
        changed=query()['point_trace']
        check('rebuild_removes_corrected_source_event',not any(o['source_id']=='S2' and o['observation_id']=='shared' for o in changed['observations']))
        check('other_activity_unchanged_after_correction',len(query('B')['point_trace']['observations'])==1)
    except BaseException as error:failure=repr(error)
    finally:
        if proc and proc.poll() is None:stop()
        (args.output/'calls.json').write_text(json.dumps(calls,ensure_ascii=False,indent=2),encoding='utf-8')
        result=dict(scope='Local signed Go/C++/fresh SQLite with independent synthetic points and events; no field accuracy claim',
            passed=sum(c['passed'] for c in checks),checks=checks,failure=failure,
            worker_sha256=hashlib.sha256(args.worker.read_bytes()).hexdigest(),service_sha256=hashlib.sha256(args.service.read_bytes()).hexdigest())
        (args.output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False))
    if failure:raise RuntimeError(failure)
    (args.output/'synthetic-example.json').write_text(json.dumps(sample,ensure_ascii=False,indent=2),encoding='utf-8')
    if args.example:args.example.write_text(json.dumps(sample,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':main()
