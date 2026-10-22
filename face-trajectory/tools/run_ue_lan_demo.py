"""Persistent, isolated LAN simulation for UE request/response integration.

The signing authority, person templates and observations are SYNTHETIC. This
does not start P1 photo detection, connect cameras, or use a production clock.
The test authority anchors a virtual UTC clock once and advances monotonically.
Only the specified physical LAN address is bound. No push endpoint is enabled.
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

P2=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(P2/'evidence/2026-10-10-activity-retention'))
from verify_core import SPACE, POLICY, vec, iso


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--bind',required=True)
    p.add_argument('--port',type=int,default=18081)
    p.add_argument('--hours',type=int,default=8)
    p.add_argument('--firewall-rule',default='')
    p.add_argument('--photo-port',type=int,default=0,help='Optional synthetic SVG photo fixture server; no real faces')
    p.add_argument('--photo-firewall-rule',default='')
    a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    virtual_epoch=int(time.time()*1000);monotonic_epoch=time.monotonic()
    now=lambda: virtual_epoch+int((time.monotonic()-monotonic_epoch)*1000)
    expires=virtual_epoch+a.hours*3600000
    activity='SYNTHETIC:'+out.name
    scope=dict(activity_id=activity,activity_num=activity,config_version=1,binding_version=1,monitoring_version=1)
    url='http://'+a.bind+':'+str(a.port)
    private=Ed25519PrivateKey.generate()
    def write(name,value):
        destination=out/name
        destination.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    def signed(body):
        raw=json.dumps(body,separators=(',',':')).encode()
        enc=lambda b:base64.urlsafe_b64encode(b).decode().rstrip('=')
        return enc(raw)+'.'+enc(private.sign(raw))
    keys=out/'public-keys.json';proof=out/'synthetic-clock-proof.txt'
    keys.write_text(json.dumps(dict(keys={'SYNTHETIC:LAN':base64.b64encode(private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()})),encoding='utf-8')
    def token(actions,globals=()):
        return signed(dict(issuer='SYNTHETIC:UE-LAN',key_id='SYNTHETIC:LAN',subject='SYNTHETIC:LAN-CLIENT',session=out.name,
                           context_id=out.name,context_generation=1,authz_revision=1,not_before_unix_ms=virtual_epoch-60000,
                           expires_unix_ms=expires,grants=[dict(activity_id=activity,actions=actions)],global_actions=list(globals)))
    admin=token(['query','config','monitor','ingest','identity'],['global_config','push_status'])
    query_token=token(['query'])
    upload_token=token(['ingest'])
    (out/'query-authorization.txt').write_text('Bearer '+query_token,encoding='utf-8')
    (out/'upload-authorization.txt').write_text('Bearer '+upload_token,encoding='utf-8')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    calls=[];process=None;failure=None;refreshes=0;photos=None;photo_thread=None
    photo_base='http://'+a.bind+':'+str(a.photo_port) if a.photo_port else None
    worker=P2/'cpp/build-ue-camera-events/face_trajectory_worker.exe'
    service=P2/'evidence/2026-10-21-ue-timeline-query/face-trajectoryd-local-test.exe'
    def call(path,body=None,bearer=None,expected=200,record=True):
        req=urllib.request.Request(url+path,data=None if body is None else json.dumps(body,ensure_ascii=False).encode(),
                                   headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+bearer} if bearer else {})})
        try:
            with opener.open(req,timeout=15) as response:status=response.status;result=json.load(response)
        except urllib.error.HTTPError as err:status=err.code;result=json.load(err)
        if record:calls.append(dict(path=path,status=status,response=result))
        if status!=expected:raise RuntimeError(path+': '+str(status)+' '+json.dumps(result,ensure_ascii=False)[:1000])
        return result
    def refresh_clock():
        nonlocal refreshes
        h=call('/health',record=False)
        # Windows timer sampling can round differently in Python and Go. Keep
        # this synthetic authority beyond the service's last verified watermark.
        stamp=h['clock'].get('stamp',{})
        proof_now=max(now(),stamp.get('now_unix_ms',0)+100)
        value=signed(dict(issuer='SYNTHETIC:UE-LAN',key_id='SYNTHETIC:LAN',challenge=h['clock']['challenge'],
                          now_unix_ms=proof_now,uncertainty_ms=0,valid_for_ms=120000,source_verified=True,
                          source='SYNTHETIC_TEST',evidence_id='SYNTHETIC:LAN-DEMO-VIRTUAL-UTC'))
        temporary=proof.with_suffix('.tmp');temporary.write_text(value,encoding='utf-8');os.replace(temporary,proof)
        refreshes+=1
    info=dict(state='STARTING',base_url=url,endpoint=url+'/v1/monitoring/timeline/query',scope=scope,
              bind=a.bind,port=a.port,source='SYNTHETIC',clock='SYNTHETIC virtual UTC; not a production time authority',
              supervisor_pid=os.getpid(),expires_unix_ms=expires,
              expires_beijing=datetime.fromtimestamp(expires/1000,timezone(timedelta(hours=8))).isoformat(timespec='seconds'),
              firewall_rule=a.firewall_rule,production_connected=False,push_enabled=False)
    info.update(photo_base_url=photo_base,photo_firewall_rule=a.photo_firewall_rule,result_delivery='BACKEND_REQUEST_RESPONSE_ONLY')
    write('run.json',info)
    try:
        if photo_base:
            fixture_paths={'/photos/profiles/priority.svg','/photos/profiles/normal.svg',
                           *{'/photos/captures/'+str(i)+'.svg' for i in range(1,8)}}
            class PhotoFixtures(BaseHTTPRequestHandler):
                def log_message(self,*_):pass
                def do_GET(self):
                    if self.path not in fixture_paths:
                        self.send_error(404);return
                    # Explicit diagram placeholder, never a fabricated face photo.
                    label=self.path.rsplit('/',1)[-1]
                    content=('<svg xmlns="http://www.w3.org/2000/svg" width="480" height="280">'
                             '<rect width="480" height="280" fill="#15263c"/>'
                             '<rect x="24" y="24" width="432" height="232" fill="none" stroke="#58b7c9"/>'
                             '<g fill="white" font-family="sans-serif" text-anchor="middle">'
                             '<text x="240" y="112" font-size="24">SYNTHETIC PHOTO FIXTURE</text>'
                             '<text x="240" y="156" font-size="20">'+label+'</text>'
                             '<text x="240" y="205" font-size="16">NO REAL PERSON / NO CAMERA INPUT</text></g></svg>').encode()
                    self.send_response(200);self.send_header('Content-Type','image/svg+xml')
                    self.send_header('Content-Length',str(len(content)));self.send_header('Cache-Control','no-store')
                    self.end_headers();self.wfile.write(content)
            photos=ThreadingHTTPServer((a.bind,a.photo_port),PhotoFixtures)
            photo_thread=threading.Thread(target=photos.serve_forever,daemon=True);photo_thread.start()
        log=(out/'service.log').open('w',encoding='utf-8')
        process=subprocess.Popen([str(service),'-worker',str(worker),'-state-file',str(out/'isolated.sqlite'),'-addr',a.bind+':'+str(a.port),
                                  '-auth-public-key-file',str(keys),'-auth-issuer','SYNTHETIC:UE-LAN',
                                  '-clock-public-key-file',str(keys),'-clock-issuer','SYNTHETIC:UE-LAN','-clock-proof-file',str(proof)],
                                 stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        log.close();info['service_pid']=process.pid
        deadline=time.monotonic()+15
        while True:
            try:call('/health',record=False);break
            except (OSError,urllib.error.URLError):
                if time.monotonic()>deadline:raise
                time.sleep(.1)
        refresh_clock()
        camera_names=['10136看台A1下入口1','10123看台A2下入口1','10126看台A3下入口1']
        points=json.loads((P2.parents[0]/'outputs/point-route-simulation-20261021/points.json').read_text(encoding='utf-8'))
        assert all(name in {r['name'] for r in points['rows']} for name in camera_names)
        cameras=[dict(camera_id='SIM-CAM-'+str(i+1),camera_name=name) for i,name in enumerate(camera_names)]
        call('/v1/activities/sync',dict(activity_id=activity,activity_num=activity,venue_id='SYNTHETIC:LAN-VENUE',config_version=1,
                                      expected_config_version=0,configuration=dict(output_mode='UE_CAMERA_EVENTS')),admin)
        call('/v1/activities/bindings',dict(activity_id=activity,config_version=1,binding_version=1,expected_binding_version=0,
                                          bindings=[dict(source_id='SYNTHETIC:CAPTURE',device_id=c['camera_id'],camera_name=c['camera_name'],
                                                         kind='CAMERA',route_rule_id='SYNTHETIC:UE-ROUTE') for c in cameras]),admin)
        for c in cameras:
            profile=dict(camera_id=c['camera_id'],profile_version='SYNTHETIC:LAN-CLOCK',verified=True,capture_semantics='DEVICE_CAPTURE',
                         source_evidence_id='SYNTHETIC:LAN-CLOCK',fixed_utc_offset_minutes=0,
                         calibrations=[dict(calibration_id='SYNTHETIC:IDEAL-CLOCK',source_evidence_id='SYNTHETIC:LAN-CLOCK',
                                            valid_from_utc_ms=virtual_epoch-86400000,valid_until_utc_ms=expires+86400000,
                                            offset_ms=0,uncertainty_ms=0,time_resolution_ms=1)])
            call('/v1/clocks/profiles',dict(profile=profile),admin)
        call('/v1/activities/monitoring',dict(activity_id=activity,config_version=1,binding_version=1,monitoring_version=1,
                                            expected_monitoring_version=0,enabled=True),admin)
        persons=[('SIM-PRIORITY-001','PRIORITY',0),('SIM-NORMAL-001','NORMAL',1)]
        for person,category,index in persons:
            display=dict(person_name='模拟重点人员甲' if category=='PRIORITY' else '模拟普通人员乙',
                         profile_photo_url=(photo_base+'/photos/profiles/'+('priority' if index==0 else 'normal')+'.svg') if photo_base else None)
            call('/v1/monitoring/persons',dict(scope,person_id=person,category=category,revision=1,expected_revision=0,
                                            evidence_id='SYNTHETIC:UPLOADED-PERSON-DATA',
                                            templates=[dict(template_id='SYNTHETIC:PHOTO:'+person,feature_space=SPACE,embedding=vec(index))],**display),admin)
        # Nominal simulated captures: repeated camera, transition and return.
        walk=[(0,0,240),(0,0,180),(0,1,120),(0,2,60),(0,1,20),(1,1,210),(1,2,90)]
        uploads=[];receipts=[]
        for number,(person_index,camera_index,seconds_before) in enumerate(walk,1):
            event='SYNTHETIC:CAPTURE:'+str(number)
            body=dict(scope,route_rule_id='SYNTHETIC:UE-ROUTE',
                      observation=dict(observation_id=event,source_id='SYNTHETIC:CAPTURE',camera_id=cameras[camera_index]['camera_id'],
                                       frame_id='SYNTHETIC:FRAME:'+str(number),feature_space=SPACE,embedding=vec(person_index),
                                       capture_photo_url=(photo_base+'/photos/captures/'+str(number)+'.svg') if photo_base else None,
                                       detection_score=.99,face_size_px=64),
                      time_record=dict(profile_version='SYNTHETIC:LAN-CLOCK',raw_time=iso(virtual_epoch-seconds_before*1000)),policy=POLICY)
            uploads.append(body);r=call('/v1/monitoring/observe',body,upload_token)
            if r['status']!='ACCEPTED':raise RuntimeError('simulation capture not accepted')
            receipts.append(dict(observation_id=event,status=r['status'],camera_event=r['camera_event']))
        write('simulated-uploads.json',uploads);write('upload-receipts.json',receipts)
        call('/v1/monitoring/timeline/query',dict(scope,person_id=persons[0][0],from_unix_ms=virtual_epoch-3600000,to_unix_ms=expires),expected=401)
        results={}
        for person,category,_ in persons:
            request=dict(scope,person_id=person,from_unix_ms=virtual_epoch-3600000,to_unix_ms=expires,limit=500)
            result=call('/v1/monitoring/timeline/query',request,query_token)
            assert result['count']==(5 if category=='PRIORITY' else 2)
            assert result['is_priority']==(category=='PRIORITY')
            assert result['has_more'] is False
            assert result['person']['person_name']==('模拟重点人员甲' if category=='PRIORITY' else '模拟普通人员乙')
            if photo_base:
                for photo_url in [result['person']['profile_photo_url'],*[row['capture_photo_url'] for row in result['timeline']]]:
                    with opener.open(photo_url,timeout=5) as photo_response:
                        assert photo_response.status==200 and b'NO REAL PERSON' in photo_response.read()
            label='priority' if category=='PRIORITY' else 'normal'
            write('query-'+label+'.json',request);write('timeline-'+label+'.json',result)
            results[person]=dict(count=result['count'],is_priority=result['is_priority'],person=result['person'],timeline=result['timeline'])
        query_request=json.loads((out/'query-priority.json').read_text(encoding='utf-8'))
        (out/'backend-query.http').write_text('POST '+info['endpoint']+'\nAuthorization: Bearer '+query_token+'\nContent-Type: application/json\n\n'+json.dumps(query_request,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        (out/'simulate-upload.http').write_text('POST '+url+'/v1/monitoring/observe\nAuthorization: Bearer '+upload_token+'\nContent-Type: application/json\n\n'+json.dumps(uploads[0],ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        status=call('/v1/push/status',bearer=admin)['push'];assert status['enabled'] is False and status['pending']==0
        info.update(state='RUNNING',simulated_uploads=len(uploads),persons=results,cameras=cameras,proof_refreshes=refreshes,last_check_unix_ms=now())
        write('bootstrap-calls.json',calls);write('run.json',info)
        (out/'README.md').write_text('# 业务后端 Wi-Fi局域网模拟联调\n\n服务：'+url+'\n\n接口：POST /v1/monitoring/timeline/query\n\n后端请求一次，返回一次 JSON，不开启推送/订阅。本次不处理UE。\n\n重点人员：SIM-PRIORITY-001，5条经过；普通人员：SIM-NORMAL-001，2条经过。返回人员姓名、档案照URL和每条抓拍照片URL、摄像头位置名称、时间线。\n\nbackend-query.http包含可直接使用的只读查询请求和测试Bearer；query-priority.json/query-normal.json为请求参数。simulate-upload.http为一次模拟抓拍上传，相同内容重试会去重。upload-receipts.json与timeline-*.json为实际回执。\n\n所有人员、特征向量、采集与钟源均为SYNTHETIC，未运行真实照片检测、未接真实大麦/摄像头/生产系统。照片URL指向本次临时图像服务（'+str(photo_base)+'），内容是明确标注无真实人脸的SVG示意图，用于测试URL可访问性；实际图片存储由采集方或业务后端提供。后台只维护测试时间证明，不周期查询或发送人员结果。\n\n测试有效至北京时间 '+info['expires_beijing']+'，到期自动结束。提前结束运行 Stop-demo.ps1。\n',encoding='utf-8')
        (out/'Stop-demo.ps1').write_text("Set-Content -LiteralPath (Join-Path $PSScriptRoot 'STOP') -Value 'requested'\nWrite-Host '已请求停止本次UE局域网模拟服务。'\n",encoding='utf-8-sig')
        refresh_due=time.monotonic()+30.5
        while not (out/'STOP').exists() and now()<expires:
            if process.poll() is not None:raise RuntimeError('algorithm service exited unexpectedly')
            if time.monotonic()>=refresh_due:
                refresh_clock()
                if not call('/health',record=False)['clock']['trusted']:raise RuntimeError('synthetic clock renewal failed')
                info.update(proof_refreshes=refreshes,last_check_unix_ms=now());write('run.json',info)
                refresh_due=time.monotonic()+30.5
            time.sleep(.5)
        info['state']='STOPPED';info['stop_reason']='REQUESTED' if (out/'STOP').exists() else 'TEST_EXPIRED'
    except BaseException as exc:
        failure=repr(exc);info.update(state='FAILED',error=failure)
    finally:
        if process and process.poll() is None:process.terminate();process.wait(timeout=20)
        if photos:photos.shutdown();photos.server_close();photo_thread.join(timeout=5)
        for rule in filter(None,[a.firewall_rule,a.photo_firewall_rule]):
            cleanup=subprocess.run(['powershell.exe','-NoProfile','-WindowStyle','Hidden','-Command',
                                    "Remove-NetFirewallRule -Name '"+rule+"' -ErrorAction SilentlyContinue"],
                                   capture_output=True,text=True,creationflags=subprocess.CREATE_NO_WINDOW)
            info['firewall_cleanup_exit_code']=cleanup.returncode
        info['ended_unix_ms']=now();write('run.json',info)
    if failure:raise SystemExit(1)


if __name__=='__main__':main()
