"""Signed local Go -> C++ -> HTTP receiver verification using saved synthetic fixtures."""
import argparse
import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

P2=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(P2/'evidence/2026-10-10-activity-retention'))
from verify_core import T0, scope, iso


def main():
    p=argparse.ArgumentParser()
    for name in ['worker','service','fixtures','output']:
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--timeline-query',action='store_true',help='Request/response timeline with backend push disabled')
    p.add_argument('--person-track',action='store_true',help='Verify v1 flat push, failed receipt retries and corrections')
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    received=[];calls=[];process=None;failure=None
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            raw=self.rfile.read(int(self.headers['Content-Length']));body=json.loads(raw)
            received.append(dict(event=body,idempotency_key=self.headers.get('Idempotency-Key'),
                authorization_ok=self.headers.get('Authorization')=='Bearer SYNTHETIC:platform-push',raw=raw.decode()))
            if a.person_track:
                self.send_response(503 if len(received)==1 else 200);self.end_headers()
                self.wfile.write(json.dumps(dict(code=200,event_id='wrong-event' if len(received)==2 else body['event_id'])).encode())
            else:
                self.send_response(204);self.end_headers()
        def log_message(self,*_):pass
    receiver=ThreadingHTTPServer(('127.0.0.1',0),Receiver)
    receiver_thread=threading.Thread(target=receiver.serve_forever,daemon=True);receiver_thread.start()
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    url='http://127.0.0.1:'+str(port)
    private=Ed25519PrivateKey.generate()
    def signed(body):
        raw=json.dumps(body,separators=(',',':')).encode()
        enc=lambda b:base64.urlsafe_b64encode(b).decode().rstrip('=')
        return enc(raw)+'.'+enc(private.sign(raw))
    keyfile=a.output/'keys.json';proof=a.output/'proof.txt'
    keyfile.write_text(json.dumps({'keys':{'SYNTHETIC':base64.b64encode(private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()}}))
    bearer=signed(dict(issuer='SYNTHETIC',key_id='SYNTHETIC',subject='SYNTHETIC:UE',session='SYNTHETIC:session',context_id='SYNTHETIC:UE',
                       context_generation=1,authz_revision=1,not_before_unix_ms=T0-100000,expires_unix_ms=T0+3600000,
                       grants=[dict(activity_id='A',actions=['query','subscribe','config','monitor','ingest','identity'])],global_actions=['global_config','push_status']))
    def call(path,body=None,auth=True,expected_status=200,method=None):
        req=urllib.request.Request(url+path,data=None if body is None else json.dumps(body,ensure_ascii=False).encode(),
                                   headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+bearer} if auth else {})},method=method)
        try:
            with urllib.request.urlopen(req,timeout=15) as res:out=json.load(res);status=res.status
        except urllib.error.HTTPError as exc:
            status=exc.code;out=json.load(exc)
        calls.append(dict(path=path,status=status,response=out))
        if status!=expected_status:raise AssertionError((path,status,out))
        return out
    paths=dict(activity_sync='/v1/activities/sync',activity_bindings='/v1/activities/bindings',activity_monitoring='/v1/activities/monitoring',
               clock_profile_put='/v1/clocks/profiles',monitoring_person_put='/v1/monitoring/persons',monitoring_observe='/v1/monitoring/observe')
    if a.person_track:paths['association_correct']='/v1/associations/correct'
    try:
        log=(a.output/'service.log').open('w')
        command=[str(a.service.resolve()),'-worker',str(a.worker.resolve()),'-state-file',str((a.output/'isolated.sqlite').resolve()),
                                  '-addr','127.0.0.1:'+str(port),'-auth-public-key-file',str(keyfile.resolve()),'-auth-issuer','SYNTHETIC',
                                  '-clock-public-key-file',str(keyfile.resolve()),'-clock-issuer','SYNTHETIC','-clock-proof-file',str(proof.resolve())]
        if not a.timeline_query:command+=['-push-url','http://127.0.0.1:'+str(receiver.server_port)+'/ue-events']
        environment=os.environ.copy()
        if a.person_track:environment['FACE_TRAJECTORY_PUSH_AUTHORIZATION']='Bearer SYNTHETIC:platform-push'
        process=subprocess.Popen(command,stdout=log,stderr=log,env=environment)
        log.close()
        deadline=time.monotonic()+10
        while True:
            try:health=call('/health',auth=False);break
            except (OSError,urllib.error.URLError):
                if time.monotonic()>deadline:raise
                time.sleep(.05)
        proof.write_text(signed(dict(issuer='SYNTHETIC',key_id='SYNTHETIC',challenge=health['clock']['challenge'],now_unix_ms=T0+100000,
                                    uncertainty_ms=0,valid_for_ms=120000,source_verified=True,source='SYNTHETIC_TEST',evidence_id='SYNTHETIC:UE-HTTP')))
        expected={};person_track_sample=None
        for item in json.loads(a.fixtures.read_text(encoding='utf-8')):
            original=item['request'];op=original['op']
            if a.person_track and op=='retention_run':break
            if a.person_track and op=='remove_template':
                request={k:v for k,v in original.items() if k!='op' and k!='template_id' and not k.startswith('_')}
                call('/v1/templates/'+original['template_id'],request,method='DELETE')
                continue
            if op not in paths or not item['response'].get('ok'):continue
            request={k:v for k,v in original.items() if k!='op' and not k.startswith('_')}
            if a.person_track and op=='monitoring_observe' and person_track_sample is None:
                person_track_sample=json.loads(json.dumps(request))
            result=call(paths[op],request)
            if op=='monitoring_observe' and result.get('camera_event'):
                e=result['camera_event'];expected[e['observation_id']]=e
                if e['observation_id']=='normal' and not a.person_track:break
        if a.person_track:
            for index,when in enumerate((40000,41000)):
                sample=json.loads(json.dumps(person_track_sample))
                sample['observation'].update(observation_id='dedup-http-'+str(index),frame_id='dedup-http-'+str(index),camera_name='设备直读入口')
                sample['time_record']['raw_time']=iso(T0+when)
                assert call('/v1/monitoring/observe',sample)['status']=='ACCEPTED'
            deadline=time.monotonic()+12
            while time.monotonic()<deadline:
                if call('/v1/push/status')['push']['pending']==0:break
                time.sleep(.05)
            assert len(received)==9,len(received)
            assert received[0]['raw']==received[1]['raw']==received[2]['raw'],'retry changed event body'
            events={x['event']['event_id']:x['event'] for x in received}
            assert len(events)==7,len(events)
            dedup=[x for x in events.values() if x['observation_id'].startswith('dedup-http-')]
            assert len(dedup)==1 and dedup[0]['observation_id']=='dedup-http-0'
            assert dedup[0]['camera_name']=='设备直读入口'
            rows=sorted((x for x in events.values() if x['observation_id']=='face-0'),key=lambda x:x['revision'])
            assert [x['revision'] for x in rows]==[1,2,3,4],rows
            assert [x['person_id'] for x in rows]==['P','Q',None,'P']
            assert [x['event_type'] for x in rows]==['UPSERT','UPSERT','RETRACT','UPSERT']
            assert all(x['capture_photo_url']=='https://photos.example.test/capture.jpg' for x in rows)
            assert all(len(x['event'])==12 and x['authorization_ok'] for x in received)
        elif a.timeline_query:
            body=dict(scope(),person_id='DAMAI-P',from_unix_ms=T0,to_unix_ms=T0+10000)
            path='/v1/monitoring/timeline/query'
            call(path,body,auth=False,expected_status=401)
            call(path,dict(body,activity_id='B'),expected_status=403)
            first=call(path,body)
            assert first['is_priority'] is True
            assert [r['observation_id'] for r in first['timeline']]==['first','repeat']
            assert all(r['camera_name']=='南门检票大棚' for r in first['timeline'])
            assert first['client_context']['context_id']=='SYNTHETIC:UE'
            assert first['has_more'] is False and first['next_after'] is None
            normal=call(path,dict(body,person_id='DAMAI-N'))
            assert normal['is_priority'] is False and normal['timeline'][0]['camera_id']=='C2'
            assert received==[]
            status=call('/v1/push/status')['push']
            assert status['pending']==0 and status['enabled'] is False
            (a.output/'example-query-response.json').write_text(json.dumps(first,ensure_ascii=False,indent=2),encoding='utf-8')
        else:
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                faces=[x for x in received if x['event'].get('result',{}).get('camera_event')]
                if len(faces)==len(expected) and call('/v1/push/status')['push']['pending']==0:break
                time.sleep(.05)
            assert len(expected)==3 and len(faces)==3,(len(expected),len(faces))
            for item in faces:
                e=item['event'];capture=e['result']['camera_event']
                assert capture==expected[capture['observation_id']]
                assert capture['camera_name']=='南门检票大棚'
                assert set(e['result'])=={'ok','status','output_mode','camera_event'}
                wanted=hashlib.sha256(json.dumps([e['activity_id'],e['event_id']],separators=(',',':')).encode()).hexdigest()
                assert item['idempotency_key']==wanted
            assert expected['first']['is_priority'] is True and expected['normal']['is_priority'] is False
        assert call('/v1/push/status')['push']['pending']==0
    except BaseException as exc:failure=repr(exc)
    finally:
        if process and process.poll() is None:process.terminate();process.wait(timeout=15)
        receiver.shutdown();receiver.server_close();receiver_thread.join(timeout=5)
        (a.output/'calls.json').write_text(json.dumps(calls,ensure_ascii=False,indent=2),encoding='utf-8')
        (a.output/'received.json').write_text(json.dumps(received,ensure_ascii=False,indent=2),encoding='utf-8')
        result=dict(passed=failure is None,failure=failure,request_only_timeline=a.timeline_query,
                    received_camera_events=sum(bool(e['event'].get('result',{}).get('camera_event')) for e in received),
                    person_track_protocol=a.person_track,received_person_track_attempts=sum('event_type' in e['event'] for e in received),
                    scope='Real signed local Go/C++/SQLite and loopback HTTP receiver; synthetic identities and clocks; no UE project or production connection')
        (a.output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False))
    if failure:raise SystemExit(1)


if __name__=='__main__':main()
