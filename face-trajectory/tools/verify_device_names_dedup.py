"""Real worker/SQLite: device name precedence and durable 5-second reporting."""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'evidence/2026-10-10-activity-retention'))
from verify_core import T0, SPACE, POLICY, vec, iso, profile, clock


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--worker',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    database=args.output/'isolated.sqlite'
    now=T0+100000
    calls,checks=[],[]
    def start():
        return subprocess.Popen([str(args.worker.resolve()),'--state-file',str(database.resolve()),
            '--push-results','true','--retention-enabled','true'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,text=True,encoding='utf-8')
    process=start()
    def stop():process.stdin.close();process.wait(timeout=20)
    def restart():
        nonlocal process
        stop();process=start()
    def call(body,ok=True):
        body=dict(body,_trusted_clock=clock(now))
        process.stdin.write(json.dumps(body,ensure_ascii=False)+'\n');process.stdin.flush()
        raw=process.stdout.readline()
        if not raw:raise RuntimeError(process.stderr.read())
        result=json.loads(raw);calls.append(dict(request=body,response=result))
        if result.get('ok')!=ok:raise AssertionError((body['op'],result))
        return result
    def check(name,condition):
        checks.append(dict(name=name,passed=bool(condition)))
        if not condition:raise AssertionError(name)
    def scope(activity='A'):
        return dict(activity_id=activity,activity_num='NUM-'+activity,config_version=1,binding_version=1,monitoring_version=1)
    def setup(activity):
        call(dict(op='activity_sync',activity_id=activity,activity_num='NUM-'+activity,venue_id='V',
            config_version=1,expected_config_version=0,configuration=dict(output_mode='PERSON_TRACK_EVENTS')))
        call(dict(op='activity_bindings',activity_id=activity,config_version=1,binding_version=1,
            expected_binding_version=0,bindings=[dict(source_id='S',device_id=camera,kind='CAMERA',
                route_rule_id='ROUTE-A',camera_name='WRONG_TABLE_NAME') for camera in ('C1','C2','C3')]))
        call(dict(op='activity_monitoring',activity_id=activity,config_version=1,binding_version=1,
            monitoring_version=1,expected_monitoring_version=0,enabled=True))
        for person,index in [('P',0),('Q',1)]:
            call(dict(scope(activity),op='monitoring_person_put',person_id=person,person_name=person,
                category='PRIORITY',revision=1,expected_revision=0,evidence_id='SYNTHETIC',
                templates=[dict(template_id='t:'+person,feature_space=SPACE,embedding=vec(index))]))
    def observe(event,when,camera='C1',person=0,name='设备真实名称',activity='A',ok=True):
        observation=dict(observation_id=event,source_id='S',camera_id=camera,frame_id=event,
            feature_space=SPACE,embedding=vec(person),detection_score=.99,face_size_px=64,camera_name=name)
        result=call(dict(scope(activity),op='monitoring_observe',route_rule_id='ROUTE-A',observation=observation,
            time_record=dict(profile_version='good',raw_time=iso(T0+when)),policy=POLICY),ok)
        if ok and result.get('status')!='ACCEPTED':raise AssertionError((event,result.get('reason_codes')))
        return result
    def correct(event,person,ok=True):
        return call(dict(scope(),op='association_correct',source_id='S',observation_id=event,
                         target_id=person,evidence_id='SYNTHETIC:correction:'+str(person)),ok)
    def head():return call(dict(op='push_next'))['event']
    def drain():
        result=[]
        while True:
            event=head()
            if event is None:break
            result.append(event['person_track_event'])
            call(dict(op='push_ack',event_id=event['event_id']))
        return result
    failure=None
    try:
        for camera in ('C1','C2','C3'):
            p=profile();p['profile']['camera_id']=camera;p['profile']['profile_id']='profile:'+camera;call(p)
        setup('A');setup('B')
        observe('first',1000);original=head()
        check('device_name_overrides_wrong_table_binding',original['person_track_event']['camera_name']=='设备真实名称')
        # Emulate the previous release's state, which had no dedup index/candidates.
        stop()
        with sqlite3.connect(database) as db:
            db.execute("DELETE FROM state WHERE ns LIKE '%person_track_candidate' OR ns LIKE '%person_track_meta'")
            db.execute("UPDATE state SET ref='' WHERE ns LIKE '%person_track_state'")
        process=start()
        observe('duplicate',2000,'C2',name='其他设备')
        check('existing_state_is_indexed_without_rewriting_pending_event',head()==original)
        observe('boundary',6000,'C3')
        check('pending_body_is_immutable_after_duplicate',head()==original)
        restart();observe('after-restart',3000,'C2')
        check('five_second_dedup_survives_restart',head()==original and len(drain())==1)
        observe('outside-window',6001,'C1',name='设备已改名')
        check('strictly_over_five_seconds_reports_new_name',drain()[0]['camera_name']=='设备已改名')
        observe('out-of-order',0,'C2')
        check('out_of_order_duplicate_is_suppressed',head() is None)
        observe('different-person',1000,'C2',1)
        check('different_people_are_not_merged',drain()[0]['person_id']=='Q')
        observe('different-activity',1000,activity='B')
        check('activity_scope_is_not_merged',drain()[0]['activity_id']=='B')
        observe('missing-device-name',14000,name=None)
        check('device_failure_never_uses_binding_name',drain()[0]['camera_name'] is None)
        timeline=call(dict(scope(),op='monitoring_timeline_query',person_id='P',from_unix_ms=T0+14000,to_unix_ms=T0+14001))
        check('debug_timeline_does_not_restore_wrong_table_name',timeline['timeline'][0]['camera_name'] is None)
        for name in (123,'','\n','x'*1025):observe('invalid-name',18000,name=name,ok=False)
        check('invalid_device_names_are_rejected',head() is None)
        # Correct/retract a representative while another camera still supports it.
        observe('representative',25000,name='原设备名称');old=drain()[0]
        observe('suppressed',26000,'C2',name='第二设备名称')
        check('second_camera_same_person_is_suppressed',head() is None)
        correct('representative','Q');events=drain()
        check('correction_promotes_remaining_evidence',[(e['observation_id'],e['person_id']) for e in events]==
              [('representative','Q'),('suppressed','P')])
        check('correction_keeps_original_device_name',events[0]['camera_name']==old['camera_name'] and events[0]['revision']==2)
        correct('suppressed','Q');events=drain()
        check('correction_into_existing_window_retracts_extra_point',len(events)==1 and events[0]['event_type']=='RETRACT')
        correct('representative',None);events=drain()
        check('retraction_promotes_suppressed_camera',[(e['observation_id'],e['event_type']) for e in events]==
              [('representative','RETRACT'),('suppressed','UPSERT')])
        check('promoted_capture_keeps_own_name_and_revision',events[1]['camera_name']=='第二设备名称' and events[1]['revision']==3)
        correct('representative',None);check('repeated_retraction_does_not_repush',head() is None)
        # Synthetic identity corrections deliberately reused P's vector for Q;
        # remove that learned template before testing independent later captures.
        correct('suppressed',None);drain()
        # 0/4/8 seconds must not form an indefinitely extending sliding window.
        observe('fixed-0',40000);drain()
        observe('fixed-4',44000,'C2');check('within_fixed_anchor_suppressed',head() is None)
        observe('fixed-8',48000,'C3');check('suppressed_capture_does_not_extend_window',len(drain())==1)
        # Two messages from one correction must roll back atomically at capacity.
        observe('capacity-rep',60000);capacity_envelope=head();drain()
        observe('capacity-duplicate',61000,'C2')
        stop()
        with sqlite3.connect(database) as db:
            before=db.execute("SELECT ns,key,value,ref FROM state WHERE ns LIKE '%person_track%' ORDER BY ns,key").fetchall()
            for i in range(255):
                db.execute('INSERT INTO state(ns,key,value,ref,track,meta) VALUES(?,?,?,?,?,?)',
                    ('push_outbox',str(9000000000000000000+i),json.dumps(capacity_envelope),'A','SYNTHETIC:'+str(i),''))
            db.execute("UPDATE state SET value='255' WHERE ns='push_meta' AND key='pending'")
        process=start()
        result=correct('capacity-rep','Q',False)
        check('multi_event_correction_respects_queue_capacity',result['error'].startswith('CAPACITY:'))
        stop()
        with sqlite3.connect(database) as db:
            after=db.execute("SELECT ns,key,value,ref FROM state WHERE ns LIKE '%person_track%' ORDER BY ns,key").fetchall()
            check('capacity_rolls_back_dedup_membership_and_revisions',before==after)
            db.execute("DELETE FROM state WHERE ns='push_outbox'")
            db.execute("UPDATE state SET value='0' WHERE ns='push_meta' AND key='pending'")
        process=start();correct('capacity-rep','Q')
        check('correction_retry_emits_exactly_two_records',len(drain())==2)
        correct('capacity-rep',None);drain()
        # Retention may remove the representative's capture slightly earlier;
        # its index must still cover surviving members and pending delivery.
        observe('retention-rep',80000);pending=head()
        observe('retention-duplicate',81000,'C2')
        now=T0+604800000+80500
        call(dict(op='retention_run',dry_run=False))
        check('retention_keeps_queued_payload_unchanged',head()==pending)
        observe('retention-late',82000,'C3')
        check('expired_representative_still_dedups_live_member',len(drain())==1)
        now=T0+604800000+90000
        call(dict(op='retention_run',dry_run=False));restart()
        with sqlite3.connect(database) as db:
            count=db.execute("SELECT count(*) FROM state WHERE ns LIKE '%person_track_state' OR ns LIKE '%person_track_candidate'").fetchone()[0]
            check('expired_dedup_state_is_reclaimed',count==0)
    except BaseException as error:failure=repr(error)
    finally:
        if process.poll() is None:stop()
        (args.output/'calls.json').write_text(json.dumps(calls,ensure_ascii=False,indent=2),encoding='utf-8')
        summary=dict(passed=sum(c['passed'] for c in checks),checks=checks,failure=failure,
                     scope='Real worker and isolated SQLite; synthetic recognition and clock; no production devices')
        (args.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(summary,ensure_ascii=False,indent=2))
    if failure:raise SystemExit(1)

if __name__=='__main__':main()
