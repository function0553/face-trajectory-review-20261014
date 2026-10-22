"""Protocol v1 against a real C++ worker and isolated SQLite, synthetic faces only."""
import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

P2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P2 / 'evidence/2026-10-10-activity-retention'))
from verify_core import T0, SPACE, POLICY, vec, iso, scope, profile, clock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    database = args.output/'isolated.sqlite'
    now, calls, checks = T0+100000, [], []
    def start():
        return subprocess.Popen([str(args.worker.resolve()), '--state-file', str(database.resolve()),
            '--push-results', 'true', '--retention-enabled', 'true'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
    process = start()
    def stop():
        process.stdin.close()
        process.wait(timeout=20)
    def call(body, ok=True):
        request = dict(body, _trusted_clock=clock(now))
        process.stdin.write(json.dumps(request, ensure_ascii=False)+'\n'); process.stdin.flush()
        line = process.stdout.readline()
        if not line: raise RuntimeError(process.stderr.read())
        result = json.loads(line); calls.append(dict(request=request,response=result))
        if result.get('ok') != ok: raise AssertionError((body.get('op'),result))
        return result
    def check(name, value):
        checks.append(dict(name=name, passed=bool(value)))
        if not value: raise AssertionError(name)
    def observe(event, index=0, **extra):
        observation = dict(observation_id=event,source_id='S',camera_id='C1',frame_id='one-frame',
            feature_space=SPACE,embedding=vec(index),detection_score=.99,face_size_px=64,
            capture_photo_url='https://photos.example.test/capture.jpg')
        observation.update(extra)
        return dict(scope(), op='monitoring_observe', route_rule_id='ROUTE-A', observation=observation,
            time_record=dict(profile_version='good',raw_time=iso(T0+1000)),policy=POLICY)
    def head(): return call(dict(op='push_next'))['event']
    def ack(e): return call(dict(op='push_ack',event_id=e['event_id']))
    def correct(target, event='face-0'):
        return call(dict(scope(),op='association_correct',source_id='S',observation_id=event,
            target_id=target,evidence_id='SYNTHETIC:verified:'+str(target)))
    def timeline(person):
        return call(dict(scope(),op='monitoring_timeline_query',person_id=person,from_unix_ms=T0,to_unix_ms=T0+5000))['timeline']
    failure=None
    try:
        call(dict(op='activity_sync',activity_id='A',activity_num='NUM-A',venue_id='V',config_version=1,
            expected_config_version=0,configuration=dict(output_mode='PERSON_TRACK_EVENTS')))
        call(dict(op='activity_bindings',activity_id='A',config_version=1,binding_version=1,
            expected_binding_version=0,bindings=[dict(source_id='S',device_id='C1',kind='CAMERA',route_rule_id='ROUTE-A',camera_name=None)]))
        call(dict(op='activity_monitoring',activity_id='A',config_version=1,binding_version=1,
            monitoring_version=1,expected_monitoring_version=0,enabled=True))
        clock_profile=profile();clock_profile['profile']['camera_id']='C1';call(clock_profile)
        first_person=dict(scope(),op='monitoring_person_put',person_id='P',person_name='测试人员',
            profile_photo_url='https://photos.example.test/profile.jpg',category='PRIORITY',revision=1,
            expected_revision=0,evidence_id='SYNTHETIC:import')
        check('photo_url_only_is_not_successful_import',not call(first_person,False)['ok'])
        for target,category,index in [('P','PRIORITY',0),('Q','PRIORITY',1),('N','NORMAL',2)]:
            request=dict(first_person,person_id=target,category=category,templates=[dict(template_id='photo:'+target,feature_space=SPACE,embedding=vec(index))])
            imported=call(request)
            check('template_is_ready_'+target,imported['recognition_ready'] is True)
            check('import_retry_is_idempotent_'+target,call(request)['duplicate'] is True)
            if target=='N':
                call(dict(scope(),op='remove_template',template_id='photo:N'))
                check('deleted_template_is_not_ready_on_historical_retry',not call(request,False)['ok'])
                call(dict(request,revision=2,expected_revision=1))
        check('registration_does_not_push',head() is None)
        request=observe('face-0')
        call(request); first=head(); wire=first['person_track_event']
        expected={'event_id','event_type','revision','activity_id','source_id','observation_id','person_id',
            'camera_id','camera_name','captured_at_unix_ms','clock_uncertainty_ms','capture_photo_url'}
        check('flat_protocol_fields',set(wire)==expected and wire['event_type']=='UPSERT' and wire['revision']==1
            and wire['person_id']=='P' and wire['source_id']=='S' and wire['observation_id']=='face-0'
            and wire['captured_at_unix_ms']==T0+1000 and wire['camera_name'] is None)
        check('retry_does_not_add_event',call(request)['duplicate'] is True and head()==first)
        ack(first)
        call(observe('face-1',index=1,capture_photo_url=None)); second=head();ack(second)
        check('same_millisecond_faces_are_distinct',second['person_track_event']['observation_id']=='face-1'
            and second['person_track_event']['captured_at_unix_ms']==wire['captured_at_unix_ms']
            and second['person_track_event']['capture_photo_url'] is None)
        call(observe('normal',2));call(observe('anonymous',3))
        held=observe('held',face_size_px=1);call(held)
        missing=observe('unknown-time');missing['time_record']=None;call(missing)
        check('normal_unknown_and_hold_are_not_pushed',head() is None)
        correct('Q'); correction=head(); c=correction['person_track_event']
        check('correction_keeps_capture_metadata_and_increments_revision',c['revision']==2 and c['person_id']=='Q'
            and c['event_id']!=wire['event_id'] and all(c[k]==wire[k] for k in expected-{'event_id','event_type','revision','person_id'}))
        check('correction_updates_query',not any(r['observation_id']=='face-0' for r in timeline('P'))
            and any(r['observation_id']=='face-0' for r in timeline('Q')))
        ack(correction);correct('Q');check('same_identity_correction_does_not_repush',head() is None)
        correct(None); retract=head();r=retract['person_track_event']
        check('retraction_full_record_and_null_person',r['revision']==3 and r['event_type']=='RETRACT' and r['person_id'] is None
            and all(r[k]==wire[k] for k in expected-{'event_id','event_type','revision','person_id'}))
        check('retraction_removes_query_point',not any(x['observation_id']=='face-0' for x in timeline('Q')))
        ack(retract);correct(None);check('retract_retry_does_not_repush',head() is None)
        stop();process=start();correct('P'); restored=head()
        check('revision_survives_restart',restored['person_track_event']['revision']==4)
        # A full outbox must roll back both attribution and the delivery revision.
        stop()
        fill_keys=[f'{9000000000000000000+i:020}' for i in range(255)]
        with sqlite3.connect(database) as db:
            for key in fill_keys:
                db.execute('INSERT INTO state(ns,key,value,ref,track,meta) VALUES(?,?,?,?,?,?)',
                    ('push_outbox',key,json.dumps(restored),'A','SYNTHETIC:capacity:'+key,''))
            db.execute("UPDATE state SET value='256' WHERE ns='push_meta' AND key='pending'")
        process=start()
        denied=call(dict(scope(),op='association_correct',source_id='S',observation_id='face-0',
            target_id='Q',evidence_id='SYNTHETIC:capacity'),False)
        check('queue_capacity_rolls_back_identity_and_delivery_revision',denied['error'].startswith('CAPACITY:')
            and any(x['observation_id']=='face-0' for x in timeline('P')) and head()==restored)
        stop()
        with sqlite3.connect(database) as db:
            db.executemany("DELETE FROM state WHERE ns='push_outbox' AND key=?",[(k,) for k in fill_keys])
            db.execute("UPDATE state SET value='1' WHERE ns='push_meta' AND key='pending'")
        process=start()
        survivor=observe('normal-survives-prune',index=2,frame_id='survivor')
        survivor['time_record']['raw_time']=iso(T0+2000)
        call(survivor)
        # Queue is authoritative even after history expiry; retention cannot rewrite or delete it.
        now=T0+604800000+1500
        call(dict(op='retention_run',dry_run=False))
        check('undelivered_event_survives_168h_cleanup_unchanged',head()==restored)
        correct('Q','normal-survives-prune')
        check('live_unpushed_capture_can_be_confirmed_after_history_compaction',
            any(x['observation_id']=='normal-survives-prune' for x in timeline('Q')))
        stop();process=start()
        check('undelivered_event_survives_restart_unchanged',head()==restored)
        ack(restored);promoted=head()
        check('first_push_after_compaction_has_original_capture',promoted['person_track_event']['revision']==1
            and promoted['person_track_event']['captured_at_unix_ms']==T0+2000
            and promoted['person_track_event']['capture_photo_url']=='https://photos.example.test/capture.jpg')
        ack(promoted);check('only_explicit_ack_removes_event',head() is None)
        (args.output/'example-event.json').write_text(json.dumps(wire,ensure_ascii=False,indent=2),encoding='utf-8')
    except BaseException as exc: failure=repr(exc)
    finally:
        if process.poll() is None: stop()
        (args.output/'calls.json').write_text(json.dumps(calls,ensure_ascii=False,indent=2),encoding='utf-8')
        result=dict(passed=sum(c['passed'] for c in checks),checks=checks,failure=failure,
            scope='Real local worker and isolated SQLite; synthetic faces and clock; no production writes')
        (args.output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False,indent=2))
    if failure: raise SystemExit(1)

if __name__=='__main__': main()

