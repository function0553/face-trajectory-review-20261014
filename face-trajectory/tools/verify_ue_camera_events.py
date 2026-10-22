"""UE camera event contract against a real worker and a fresh local SQLite.

Catches: building routes in event mode; losing names/times/priority; trusting
unconfirmed identity; duplicate reports; wrong activity/source camera binding.
No real faces, cameras, Damai account or production service is used.
"""
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
    process = subprocess.Popen([str(args.worker.resolve()), '--state-file', str(database.resolve()), '--push-results', 'true', '--retention-enabled', 'true'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
    calls, checks = [], []
    trusted_now=T0+100000
    def call(body, ok=True):
        request = dict(body, _trusted_clock=clock(trusted_now))
        process.stdin.write(json.dumps(request, ensure_ascii=False)+'\n'); process.stdin.flush()
        line = process.stdout.readline()
        if not line:
            raise RuntimeError(process.stderr.read())
        result = json.loads(line)
        calls.append(dict(request=request,response=result))
        if result.get('ok') != ok:
            raise AssertionError((body.get('op'),result))
        return result
    def check(name, value):
        checks.append(dict(name=name,passed=bool(value)))
        if not value:
            raise AssertionError(name)
    def binding(camera, name):
        return dict(source_id='S', device_id=camera, kind='CAMERA', route_rule_id='ROUTE-A',camera_name=name)
    def observe(event, index=0, camera='C1', dt=1000, **more):
        observation=dict(observation_id=event,source_id='S',camera_id=camera,frame_id='frame:'+event,
                         feature_space=SPACE,embedding=vec(index),detection_score=.99,face_size_px=64,**more)
        return dict(scope(),op='monitoring_observe',route_rule_id='ROUTE-A',observation=observation,
                    time_record=dict(profile_version='good',raw_time=iso(T0+dt)),policy=POLICY)
    def pending():
        return call(dict(op='push_status'))['push']['pending']
    def next_event():
        return call(dict(op='push_next'))['event']
    def drain():
        events=[]
        while True:
            e=next_event()
            if e is None:
                break
            events.append(e)
            call(dict(op='push_ack',event_id=e['event_id']))
        return events
    failure = None
    try:
        call(dict(op='activity_sync',activity_id='A',activity_num='NUM-A',venue_id='V',config_version=1,expected_config_version=0,
                  configuration=dict(output_mode='UE_CAMERA_EVENTS')))
        bad=dict(op='activity_bindings',activity_id='A',config_version=1,binding_version=1,expected_binding_version=0,
                 bindings=[dict(source_id='S',device_id='C1',kind='CAMERA',route_rule_id='ROUTE-A')])
        check('event_mode_requires_authoritative_camera_name',not call(bad,False)['ok'])
        call(dict(bad, bindings=[binding('C1','南门检票大棚'),binding('C2','南门检票大棚')]))
        call(dict(op='activity_monitoring',activity_id='A',config_version=1,binding_version=1,monitoring_version=1,
                  expected_monitoring_version=0,enabled=True))
        for camera in ['C1','C2']:
            p=profile()['profile'];p['camera_id']=camera
            call(dict(op='clock_profile_put',profile=p))
        for target, category, i in [('DAMAI-P','PRIORITY',0),('DAMAI-N','NORMAL',1)]:
            call(dict(scope(),op='monitoring_person_put',person_id=target,category=category,revision=1,expected_revision=0,
                      evidence_id='SYNTHETIC:damai-upload',templates=[dict(template_id='photo:'+target,feature_space=SPACE,embedding=vec(i))]))
        drain()
        first_request=observe('first',camera_name='SPOOFED_NAME')
        first=call(first_request)
        event=first['camera_event']
        check('confirmed_face_reports_name_time_and_priority_without_graph_or_scene',
              first['status']=='ACCEPTED' and event['camera_name']=='南门检票大棚' and event['captured_at_unix_ms']==T0+1000
              and event['person_id']=='DAMAI-P' and event['is_priority'] is True and event['category']=='PRIORITY')
        check('route_rebuild_is_skipped',first['trajectories']==[] and first['output_mode']=='UE_CAMERA_EVENTS')
        check('source_ids_are_external',event['source_id']=='S' and event['observation_id']=='first')
        push=next_event()
        check('compact_ue_push_has_no_trajectory_or_face_candidates',
              push['result']['camera_event']==event and set(push['result'])=={'ok','status','output_mode','camera_event'})
        before=pending(); retry=call(first_request)
        check('duplicate_capture_does_not_report_twice',retry['duplicate'] is True and pending()==before and retry['camera_event']==event)
        repeat=call(observe('repeat',dt=2000))
        check('new_capture_same_camera_is_reported',repeat['camera_event']['observation_id']=='repeat' and pending()==before+1)
        normal=call(observe('normal',index=1,camera='C2',dt=3000))
        check('normal_damai_person_is_reported_and_not_marked_priority',normal['camera_event']['person_id']=='DAMAI-N'
              and normal['camera_event']['is_priority'] is False and normal['camera_event']['category']=='NORMAL')
        check('same_named_devices_keep_distinct_ids',normal['camera_event']['camera_id']=='C2' and event['camera_id']=='C1')
        anonymous=call(observe('unregistered',index=2,camera='C2',dt=4000))['camera_event']
        check('unregistered_person_is_never_invented_as_priority',anonymous['person_id'].startswith('auto:')
              and anonymous['category']=='UNREGISTERED' and anonymous['is_priority'] is False)
        n=pending(); held=observe('unknown-time');held['time_record']=None
        result=call(held)
        check('untrusted_time_is_held_and_not_sent_to_ue',result['status']=='HOLD' and result.get('camera_event') is None and pending()==n)
        low=observe('low-quality');low['observation']['face_size_px']=1
        result=call(low)
        check('unconfirmed_face_is_not_sent_to_ue',result['status']=='HOLD' and result.get('camera_event') is None and pending()==n)
        unbound=observe('unbound',camera='UNKNOWN')
        result=call(unbound)
        check('unbound_camera_is_not_reported',result['status']=='HOLD' and result.get('camera_event') is None and pending()==n)
        updates=call(dict(scope(),op='activity_results',after_sequence=0,limit=100))['events']
        check('ue_subscription_carries_camera_events',len(updates)==4 and all(e['camera_event']['camera_name']=='南门检票大棚' for e in updates))
        with sqlite3.connect(database) as db:
            rows=db.execute('SELECT ns,key FROM state').fetchall()
        check('no_route_snapshot_is_persisted',not any('trajectory' in ns or 'point_graph' in ns or 'site_scene' in ns for ns,key in rows))
        check('route_query_is_explicitly_disabled',not call(dict(scope(),op='point_trace',target_id='DAMAI-P'),False)['ok'])
        # A profile update changes only future event flags; both classes still match
        # against all templates with the same identity thresholds.
        call(dict(scope(),op='monitoring_person_put',person_id='DAMAI-N',category='PRIORITY',revision=2,expected_revision=1,evidence_id='SYNTHETIC:damai-update'))
        changed=call(observe('priority-update',index=1,camera='C2',dt=5000))
        check('damai_category_update_marks_next_appearance',changed['camera_event']['is_priority'] is True
              and changed['camera_event']['person_revision']==2)
        # Revisions and activity scoping continue to be enforced.
        stale=observe('stale');stale['binding_version']=0
        check('stale_binding_is_held',call(stale)['status']=='HOLD')
        check('all_notifications_have_no_route_geometry',all('route_waypoints' not in json.dumps(e) for e in drain()))
        live=call(observe('live-during-prune',dt=7000))['camera_event']
        trusted_now=T0+604800000+2000
        call(dict(op='retention_run',dry_run=False))
        check('live_ue_push_survives_unrelated_history_pruning',next_event()['result']['camera_event']==live)
        updated=call(dict(scope(),op='activity_results',after_sequence=0,limit=100))['events']
        check('live_ue_subscription_survives_history_pruning',any(e.get('camera_event')==live for e in updated))
        (args.output/'example-camera-event.json').write_text(json.dumps(push,ensure_ascii=False,indent=2),encoding='utf-8')
    except BaseException as exc:
        failure=repr(exc)
    finally:
        process.stdin.close();process.wait(timeout=20)
        (args.output/'calls.json').write_text(json.dumps(calls,ensure_ascii=False,indent=2),encoding='utf-8')
        summary=dict(passed=sum(c['passed'] for c in checks),checks=checks,failure=failure,
                     scope='Real local C++ worker and fresh SQLite; synthetic people, features and clocks')
        (args.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(summary,ensure_ascii=False,indent=2))
    if failure:
        raise SystemExit(1)


if __name__=='__main__':
    main()
