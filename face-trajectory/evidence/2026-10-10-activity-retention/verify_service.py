"""Real Go -> C++ -> fresh SQLite HTTP contract checks. Synthetic signing only."""
import argparse, base64, copy, hashlib, json, socket, subprocess, time, urllib.request, urllib.error
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from verify_core import T0, scope, profile, observation, vec, SPACE

def main():
    p=argparse.ArgumentParser();p.add_argument('--service',type=Path,required=True);p.add_argument('--worker',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.output.exists():raise RuntimeError('fresh output directory required')
    a.output.mkdir(parents=True);checks=[];calls=[];proc=None;failure=None
    key=Ed25519PrivateKey.generate();public=base64.b64encode(key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()
    keyfile=a.output/'public-keys.json';keyfile.write_text(json.dumps({'keys':{'synthetic':public}}))
    proof=a.output/'clock-proof.txt';db=a.output/'isolated.sqlite'
    s=socket.socket();s.bind(('127.0.0.1',0));port=s.getsockname()[1];s.close();url='http://127.0.0.1:'+str(port)
    def signed(body):
        data=json.dumps(body,separators=(',',':')).encode();enc=lambda b:base64.urlsafe_b64encode(b).decode().rstrip('=')
        return enc(data)+'.'+enc(key.sign(data))
    def token(activity='A',actions=None,context='viewer-A',generation=1):
        return signed(dict(issuer='SYNTHETIC',key_id='synthetic',subject=context,session=context+'-session',context_id=context,context_generation=generation,authz_revision=1,
            not_before_unix_ms=T0-100000,expires_unix_ms=T0+3600000,grants=[{'activity_id':activity,'actions':actions or ['query','subscribe','config','monitor','ingest','identity']}],global_actions=['global_config','push_status','retention']))
    def call(path,body=None,bearer=None,expected=200,record=True):
        req=urllib.request.Request(url+path,data=None if body is None else json.dumps({k:v for k,v in body.items() if k!='op'}).encode(),headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+bearer} if bearer else {})})
        try:
            with urllib.request.urlopen(req,timeout=10) as r:status=r.status;out=json.load(r)
        except urllib.error.HTTPError as e:status=e.code;out=json.load(e)
        if record:calls.append({'path':path,'request':body,'status':status,'response':out})
        if status!=expected:raise AssertionError((path,status,expected,out))
        return out
    def check(name,condition):
        checks.append({'name':name,'passed':bool(condition)})
        if not condition:raise AssertionError(name)
    def start():
        log=(a.output/('service-'+str(len(checks))+'.log')).open('w')
        process=subprocess.Popen([str(a.service.resolve()),'-worker',str(a.worker.resolve()),'-state-file',str(db.resolve()),'-addr','127.0.0.1:'+str(port),'-auth-public-key-file',str(keyfile.resolve()),'-auth-issuer','SYNTHETIC','-clock-public-key-file',str(keyfile.resolve()),'-clock-issuer','SYNTHETIC','-clock-proof-file',str(proof.resolve()),'-push-results','-retention-enabled','-retention-interval','1s'],stdout=log,stderr=log)
        for _ in range(100):
            try:
                h=call('/health',record=False);break
            except (OSError,urllib.error.URLError):time.sleep(.05)
        else:raise RuntimeError('service did not start')
        challenge=h['clock']['challenge']
        proof.write_text(signed(dict(issuer='SYNTHETIC',key_id='synthetic',challenge=challenge,now_unix_ms=T0+len(checks)*100,uncertainty_ms=0,valid_for_ms=120000,source_verified=True,source='SYNTHETIC_TEST',evidence_id='SYNTHETIC:local-only')))
        return process
    def stop():proc.terminate();proc.wait(15)
    try:
        proc=start();A=token();B=token('B',context='viewer-B');query=token(actions=['query'])
        check('missing_identity_rejected',call('/v1/activities/get',{'activity_id':'A'},expected=401)['ok'] is False)
        call('/v1/clocks/profiles',profile(),A)
        for activity,bearer in [('A',A),('B',B)]:
            r=call('/v1/activities/sync',dict(activity_id=activity,activity_num='NUM-'+activity,venue_id='VENUE',config_version=1,expected_config_version=0),bearer)
            check('create_stopped_'+activity,r['enabled'] is False)
            call('/v1/activities/bindings',dict(activity_id=activity,config_version=1,binding_version=1,expected_binding_version=0,bindings=[{'source_id':'S','device_id':'C','kind':'CAMERA','route_rule_id':'ROUTE-'+activity}]),bearer)
            call('/v1/activities/monitoring',dict(activity_id=activity,config_version=1,binding_version=1,monitoring_version=1,expected_monitoring_version=0,enabled=True),bearer)
            call('/v1/monitoring/persons',dict(scope(activity),person_id='PERSON',category='PRIORITY' if activity=='A' else 'NORMAL',revision=1,expected_revision=0,evidence_id='SYNTHETIC',templates=[{'template_id':'REGISTERED','feature_space':SPACE,'embedding':vec(0)}]),bearer)
        for path,body in [('/v1/activities/get',{'activity_id':'B'}),('/v1/monitoring/get',dict(scope('B'),target_id='PERSON')),('/v1/subscriptions/open',scope('B')),('/v1/activities/monitoring',dict(activity_id='B',enabled=False))]:
            check('cross_activity_denied_'+path,call(path,body,A,403)['ok'] is False)
        check('query_cannot_start',call('/v1/activities/monitoring',dict(activity_id='A'),query,403)['ok'] is False)
        check('activity_query_mapping_conflict_rejected',call('/v1/activities/get',dict(activity_id='A',activity_num='NUM-B'),A,409)['error_code']=='SOURCE_CONFLICT')
        check('monitoring_query_mapping_conflict_rejected',call('/v1/monitoring/get',dict(scope(),activity_num='NUM-B',target_id='PERSON'),A,409)['error_code']=='SOURCE_CONFLICT')
        from concurrent.futures import ThreadPoolExecutor
        def parallel_query(index):
            activity='A' if index%2==0 else 'B';bearer=A if activity=='A' else B
            reply=call('/v1/activities/get',{'activity_id':activity},bearer)
            return reply['activity_id']==activity and reply['client_context']['context_id']=='viewer-'+activity
        with ThreadPoolExecutor(max_workers=8) as clients:
            check('simultaneous_users_context_isolation',all(clients.map(parallel_query,range(24))))
        sub=call('/v1/subscriptions/open',scope(),A)
        first=call('/v1/monitoring/observe',observation(),A);other=call('/v1/monitoring/observe',observation(a='B'),B)
        check('real_worker_results_scoped',first['association']['target_id']==other['association']['target_id']=='PERSON' and first['activity_id']=='A' and other['activity_id']=='B')
        check('same_retry_no_effect',call('/v1/monitoring/observe',observation(),A)['duplicate'])
        changed=observation();changed['observation']['embedding']=vec(1)
        check('changed_retry_conflict',call('/v1/monitoring/observe',changed,A,409)['ok'] is False)
        poll=call('/v1/subscriptions/poll',{'subscription_id':sub['subscription_id'],'generation':sub['generation']},A)
        check('subscription_actual_notification',len(poll['events'])==1 and poll['events'][0]['activity_id']=='A' and poll['events'][0]['status']=='ACCEPTED')
        # Test client now selects B, rejects delayed A replies and notifications.
        client={'activity_id':'B','context_id':'viewer-B','context_generation':1}
        accept=lambda r:r.get('activity_id')==client['activity_id'] and r.get('client_context',{}).get('context_id')==client['context_id'] and r.get('client_context',{}).get('context_generation')==client['context_generation']
        check('client_switch_discards_old_reply',not accept(first) and accept(other))
        check('client_switch_discards_old_push',not accept(poll))
        check('other_session_cannot_poll',call('/v1/subscriptions/poll',{'subscription_id':sub['subscription_id'],'generation':sub['generation']},B,403)['ok'] is False)
        call('/v1/subscriptions/close',{'subscription_id':sub['subscription_id'],'generation':sub['generation']},A)
        check('logout_does_not_stop_monitor',call('/v1/activities/get',{'activity_id':'A'},A)['enabled'] is True)
        call('/v1/activities/monitoring',dict(activity_id='A',config_version=1,binding_version=1,monitoring_version=2,expected_monitoring_version=1,enabled=False),A)
        check('stop_A_leaves_B',call('/v1/activities/get',{'activity_id':'B'},B)['enabled'] is True)
        stop();proc=start()
        check('restart_lifecycle_A',call('/v1/activities/get',{'activity_id':'A'},A)['enabled'] is False)
        check('restart_lifecycle_B',call('/v1/activities/get',{'activity_id':'B'},B)['enabled'] is True)
        check('restart_idempotency',call('/v1/monitoring/observe',observation(a='B'),B)['duplicate'])
        aged=observation('SERVICE_EXPIRED_SENTINEL','B',t=T0-604800000+10000)
        check('old_but_window_capture_accepted',call('/v1/monitoring/observe',aged,B)['status']=='ACCEPTED')
        h=call('/health')
        proof.write_text(signed(dict(issuer='SYNTHETIC',key_id='synthetic',challenge=h['clock']['challenge'],now_unix_ms=T0+30000,uncertainty_ms=0,valid_for_ms=120000,source_verified=True,source='SYNTHETIC_TEST',evidence_id='SYNTHETIC:advance-boundary')))
        import sqlite3
        def rows():
            with sqlite3.connect(db) as conn:return conn.execute('SELECT ns,key,value,ref,track,meta FROM state ORDER BY ns,key').fetchall()
        before=rows();dry=call('/v1/retention/run',{'dry_run':True},B)
        check('http_dry_run_no_state_changes',rows()==before and dry['dry_run'])
        cleaned=call('/v1/retention/run',{'dry_run':False},B)
        check('http_real_cleanup_reports',cleaned['complete'] and cleaned['maintenance_status']=='COMPLETE' and cleaned['after_physical']['wal_bytes']==0)
        check('http_cleanup_payload_removed',not any('SERVICE_EXPIRED_SENTINEL' in value for row in rows() for value in row))
        check('http_expired_retry_no_restore',call('/v1/monitoring/observe',aged,B)['status']=='EXPIRED')
        valid=call('/v1/trajectories/get',dict(scope('B'),target_id='PERSON'),B)
        check('http_retains_other_valid_points',len(valid['trajectory']['evidence'])==1)
        status=call('/v1/retention/status',bearer=B)
        check('http_retention_observable',status['retention']['enabled'] and status['retention']['successes']>=2)
        attempts=status['retention']['attempts']
        for _ in range(40):
            time.sleep(.05);tick=call('/v1/retention/status',bearer=B,record=False)
            if tick['retention']['attempts']>attempts and not tick['retention']['running']:break
        check('periodic_real_cleanup_executes',tick['retention']['attempts']>attempts and tick['retention']['last_dry_run'] is False and tick['retention']['last_result']['complete'])
    except BaseException as e:failure=repr(e)
    finally:
        if proc and proc.poll() is None:stop()
        (a.output/'calls.json').write_text(json.dumps(calls,ensure_ascii=False,indent=2))
        result={'scope':'Local real Go/C++/isolated SQLite, synthetic test identity and UTC signer only','passed':sum(c['passed'] for c in checks),'checks':checks,'failure':failure,'service_sha256':hashlib.sha256(a.service.read_bytes()).hexdigest(),'worker_sha256':hashlib.sha256(a.worker.read_bytes()).hexdigest()}
        (a.output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result))
    if failure:raise RuntimeError(failure)
if __name__=='__main__':main()
