"""Repeatable synthetic stage-1 checks against real C++/isolated SQLite.
No camera access, production writes or identity/capacity acceptance.
"""
import argparse, copy, hashlib, json, sqlite3, subprocess
from datetime import datetime, timezone
from pathlib import Path

T0=1791626400000
SPACE={'recognition_model_sha256':'a'*64,'detector_model_sha256':'b'*64,'detector_profile':'SYNTHETIC',
       'alignment_version':'SYNTHETIC','preprocessing_version':'SYNTHETIC','output_version':'SYNTHETIC-512'}
POLICY={'policy_id':'SYNTHETIC:activity-v1','match_threshold':.85,'new_target_threshold':.25,
        'min_margin':.1,'template_update_threshold':.9,'max_template_similarity':.999,
        'min_detection_score':.5,'min_face_size_px':32,'require_topology':False,'topology_is_complete':False}
GRAPH={'version':'SYNTHETIC:unmapped','edges':[]}
def iso(t): return datetime.fromtimestamp(t/1000,timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def vec(i): return [1.0 if j==i else 0.0 for j in range(512)]
def clock(now=T0): return {'verified':True,'now_unix_ms':now,'uncertainty_ms':0,'valid_until_unix_ms':now+60000,'evidence_id':'SYNTHETIC:reference'}
def scope(a='A',**more): return dict(activity_id=a,activity_num='NUM-'+a,config_version=1,binding_version=1,monitoring_version=1,**more)
def profile(name='good',error=0,offset=0,verified=True):
    return {'op':'clock_profile_put','profile':{'camera_id':'C','profile_version':name,'verified':verified,'capture_semantics':'DEVICE_CAPTURE',
      'source_evidence_id':'SYNTHETIC:clock','fixed_utc_offset_minutes':0,
      'calibrations':[{'calibration_id':'synthetic','source_evidence_id':'SYNTHETIC:clock',
      'valid_from_utc_ms':T0-10*86400000,'valid_until_utc_ms':T0+10*86400000,'offset_ms':offset,'uncertainty_ms':error,'time_resolution_ms':1}]}}
def observation(event='same',a='A',source='S',t=T0,i=0,profile_name='good'):
    return dict(scope(a),op='monitoring_observe',route_rule_id='ROUTE-'+a,
      observation={'observation_id':event,'source_id':source,'camera_id':'C','frame_id':'frame:'+event,
        'feature_space':SPACE,'embedding':vec(i),'detection_score':.99,'face_size_px':64},
      time_record={'profile_version':profile_name,'raw_time':iso(t)},policy=POLICY,graph=GRAPH)
def gate(event,face,t,a='A',direction='EXIT',profile_name='good'):
    return dict(scope(a),op='monitoring_gate_event',route_rule_id='ROUTE-'+a,event_id=event,source_id='G',gate_id='GATE',direction=direction,
      binding={'source_id':'S','observation_id':face,'evidence_id':'SYNTHETIC:same-passage'},
      time_record={'camera_id':'C','profile_version':profile_name,'raw_time':iso(t)})

def main():
    p=argparse.ArgumentParser();p.add_argument('--worker',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.output.exists():raise RuntimeError('fresh output required')
    a.output.mkdir(parents=True);dbpath=a.output/'isolated.sqlite';calls=[];checks=[];worker=None
    def start():return subprocess.Popen([str(a.worker.resolve()),'--state-file',str(dbpath.resolve()),'--push-results','true'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
    def stop():
        worker.stdin.close();worker.wait(15)
        if worker.returncode:raise RuntimeError(worker.stderr.read())
    # This fixture includes captures through T0+70s. The signed reference
    # therefore follows them; future-capture rejection is audited separately.
    def call(r,ok=True,now=T0+100000):
        r=copy.deepcopy(r);r['_trusted_clock']=clock(now)
        worker.stdin.write(json.dumps(r)+'\n');worker.stdin.flush();text=worker.stdout.readline()
        if not text:raise RuntimeError(worker.stderr.read())
        out=json.loads(text);calls.append({'request':r,'response':out})
        if out.get('ok')!=ok:raise AssertionError((r,out))
        return out
    def check(name,condition):
        checks.append({'name':name,'passed':bool(condition)})
        if not condition:raise AssertionError(name)
    def get(activity='A'):return call(dict(scope(activity),op='monitoring_get',target_id='PERSON'))['monitoring']
    worker=start();failure=None
    try:
        for r in [profile(),profile('over60',offset=60001),profile('unverified',verified=False),profile('overlap',error=30000)]:call(r)
        check('explicit_activity_required',call({'op':'monitoring_get','target_id':'PERSON'},False)['error'].startswith('ACTIVITY_REQUIRED'))
        for activity in ['A','B']:
            created=call({'op':'activity_sync','activity_id':activity,'activity_num':'NUM-'+activity,'venue_id':'VENUE','config_version':1,'expected_config_version':0})
            check('create_stopped_'+activity,created['enabled'] is False)
            r=observation('before-start',activity)
            check('creation_not_start_'+activity,call(r)['status']=='HOLD')
            bindings=[{'source_id':s,'device_id':d,'kind':kind,'route_rule_id':'ROUTE-'+activity} for s,d,kind in [('S','C','CAMERA'),('S2','C','CAMERA'),('G','GATE','GATE')]]
            call({'op':'activity_bindings','activity_id':activity,'config_version':1,'binding_version':1,'expected_binding_version':0,'bindings':bindings})
            call({'op':'activity_monitoring','activity_id':activity,'config_version':1,'binding_version':1,'monitoring_version':1,'expected_monitoring_version':0,'enabled':True})
            call(dict(scope(activity),op='monitoring_person_put',person_id='PERSON',category='PRIORITY' if activity=='A' else 'NORMAL',revision=1,expected_revision=0,evidence_id='SYNTHETIC:enrolled',templates=[{'template_id':'REGISTERED','feature_space':SPACE,'embedding':vec(0)}]))
        first=call(observation());other=call(observation(a='B'))
        check('same_person_cross_activity',first['association']['target_id']==other['association']['target_id']=='PERSON' and first['activity_id']!=other['activity_id'])
        check('query_authoritative_mapping_conflict_rejected',all(call(dict(scope(),activity_num='NUM-B',op=op,target_id='PERSON',after_sequence=0),False)['error_code']=='SOURCE_CONFLICT' for op in ['monitoring_get','activity_get','activity_results']))
        check('independent_categories',get()['category']=='PRIORITY' and get('B')['category']=='NORMAL')
        check('same_event_other_source',call(observation(source='S2'))['status']=='ACCEPTED')
        check('same_key_retry',call(observation())['duplicate'] is True)
        bad=observation();bad['observation']['embedding']=vec(1)
        check('same_key_changed_conflict',call(bad,False)['error_code']=='SOURCE_CONFLICT')
        check('time_over60_hold',call(observation('bad-clock',profile_name='over60'))['status']=='HOLD')
        check('unverified_time_hold',call(observation('unverified-clock',profile_name='unverified'))['status']=='HOLD')
        unknown=observation('unknown-clock');unknown['time_record']=None
        check('unknown_time_hold',call(unknown)['status']=='HOLD')
        wrong=observation('bad-route');wrong['route_rule_id']='WRONG'
        check('shared_device_no_fanout',call(wrong)['status']=='HOLD')
        missing=observation('unknown-activity',a='UNKNOWN')
        check('unknown_activity_reject',call(missing,False)['error'].startswith('UNKNOWN_ACTIVITY'))
        call(observation('exit-face',t=T0+10000));closed=call(gate('EXIT','exit-face',T0+10000))
        check('A_exit_closes_A',closed['monitoring']['status']=='CLOSED')
        check('A_exit_leaves_B',get('B')['status']=='ACTIVE')
        call(observation('reentry',t=T0+20000));call(gate('late-exit','exit-face',T0+10001))
        check('late_exit_not_close_new_session',get()['status']=='ACTIVE' and len(get()['sessions'])==2)
        call(observation('uncertain-face',t=T0+60000,profile_name='overlap'))
        call(gate('uncertain-exit','uncertain-face',T0+60000,profile_name='overlap'))
        call(observation('overlap-inside',t=T0+70000,profile_name='overlap'))
        check('admitted_overlap_preserved',get()['status']=='AMBIGUOUS')
        call({'op':'activity_monitoring','activity_id':'A','config_version':1,'binding_version':1,'monitoring_version':2,'expected_monitoring_version':1,'enabled':False})
        check('stopped_late_intake_hold',call(observation('after-stop'))['status']=='HOLD')
        check('stop_A_not_B',get('B')['status']=='ACTIVE')
        oldB=get('B');stop();worker=start()
        check('restart_isolated_monitoring',get('B')==oldB)
        check('restart_stopped_state',call({'op':'activity_get','activity_id':'A'})['enabled'] is False)
        check('restart_retry',call(observation(a='B'))['duplicate'] is True)
        old=call(dict(scope('B'),op='trajectory_get',target_id='PERSON'))['trajectory']
        check('activity_trajectory_only_B',len(old['evidence'])==1)
        changed={'op':'activity_sync','activity_id':'B','activity_num':'NUM-B','venue_id':'VENUE','config_version':2,'expected_config_version':1,'configuration':{'plan_end_unix_ms':T0-1}}
        call(changed)
        check('planned_end_not_stop',call({'op':'activity_get','activity_id':'B'})['enabled'] is True)
        check('stale_config_isolated',call(observation('stale-config','B'))['status']=='HOLD')
        check('mapping_immutable',call(dict(changed,config_version=3,expected_config_version=2,activity_num='NUM-A'),False)['error_code']=='SOURCE_CONFLICT')
        with sqlite3.connect(dbpath) as db:
            namespaces=[x[0] for x in db.execute('SELECT DISTINCT ns FROM state')]
            check('physical_activity_namespaces',json.dumps(['activity','A','observation'],separators=(',',':')) in namespaces and json.dumps(['activity','B','observation'],separators=(',',':')) in namespaces)
            check('no_legacy_observation_write',db.execute("SELECT count(*) FROM state WHERE ns='observation'").fetchone()[0]==0)
        check('raw_replay_bypass_denied',call(dict(scope('B'),op='association_observe'),False)['error_code']=='SOURCE_CONFLICT')
        revised=dict(scope('B'),config_version=2)
        anon_a=observation('anonymous',i=3);anon_a['monitoring_version']=2
        # A is explicitly stopped: no anonymous identity may be created there.
        check('stopped_activity_no_anonymous',call(anon_a)['status']=='HOLD')
        anon_b=observation('anonymous','B',i=3);anon_b['config_version']=2
        anon=call(anon_b)
        check('unregistered_anonymous_trajectory',anon['association']['target_id'].startswith('auto:') and anon['monitoring']['category']=='UNREGISTERED')
        check('anonymous_context_retains_business_id',anon['activity_id']=='B' and anon['monitoring']['target_id']==anon['association']['target_id'])
        bindings=[{'source_id':'S','device_id':'C','kind':'CAMERA','route_rule_id':'ROUTE-B2'}]
        call({'op':'activity_bindings','activity_id':'B','config_version':2,'binding_version':2,'expected_binding_version':1,'bindings':bindings})
        check('old_binding_new_event_quarantined',call(dict(anon_b,observation=dict(anon_b['observation'],observation_id='old-binding')))['status']=='HOLD')
        fresh=dict(observation('new-binding','B',t=T0+1000,i=3),config_version=2,binding_version=2,route_rule_id='ROUTE-B2')
        check('new_binding_ingests_without_code_change',call(fresh)['status']=='ACCEPTED')
        conflict=dict(fresh);conflict['activity_num']='NUM-A'
        conflict['observation']=dict(fresh['observation'],observation_id='mapping-conflict')
        check('event_authoritative_mapping_conflict_hold',call(conflict)['status']=='HOLD')
        unknown['_trusted_clock']=clock()
        worker.stdin.write(json.dumps(dict(unknown,_trusted_clock=dict(clock(),verified=False)))+'\n');worker.stdin.flush()
        check('untrusted_server_reference_denied',json.loads(worker.stdout.readline())['ok'] is False)
        with sqlite3.connect(dbpath) as db:
            ns=json.dumps(['activity','B','activity_intake'],separators=(',',':'))
            rec=json.loads(db.execute('SELECT value FROM state WHERE ns=? AND key=?',(ns,json.dumps(['S','anonymous'],separators=(',',':')))).fetchone()[0])
            original=copy.deepcopy(anon_b);original.pop('op')
            check('sha256_tombstone_fingerprint',rec['fingerprint']==hashlib.sha256(json.dumps(original,sort_keys=True,separators=(',',':')).encode()).hexdigest())
    except BaseException as e:failure=repr(e)
    finally:
        if worker.poll() is None:stop()
        (a.output/'calls.json').write_text(json.dumps(calls,ensure_ascii=False,indent=2),encoding='utf-8')
        result={'scope':'SYNTHETIC local C++ and isolated SQLite; stage1 only','checks':checks,'passed':sum(c['passed'] for c in checks),'failure':failure,'state_path':str(dbpath.resolve()),'worker_sha256':hashlib.sha256(a.worker.read_bytes()).hexdigest()}
        (a.output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False))
    if failure:raise RuntimeError(failure)
if __name__=='__main__':main()
