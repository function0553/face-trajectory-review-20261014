"""Backend pull contract: real worker/SQLite, synthetic persons and image URLs."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

P2=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(P2/'evidence/2026-10-10-activity-retention'))
from verify_core import T0,SPACE,POLICY,vec,iso,scope,profile,clock

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--worker',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    a=parser.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    process=subprocess.Popen([str(a.worker.resolve()),'--state-file',str((a.output/'isolated.sqlite').resolve()),'--retention-enabled','true'],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
    calls=[];checks=[];failure=None;now=T0+100000
    def call(body,ok=True):
        request=dict(body,_trusted_clock=clock(now))
        process.stdin.write(json.dumps(request,ensure_ascii=False)+'\n');process.stdin.flush()
        reply=json.loads(process.stdout.readline())
        calls.append(dict(request=request,response=reply))
        assert reply.get('ok')==ok,(body['op'],reply)
        return reply
    def check(name,value):
        checks.append(dict(name=name,passed=bool(value)))
        assert value,name
    def person(target='P1',revision=1,expected_revision=0,**extra):
        return dict(scope(),op='monitoring_person_put',person_id=target,category='PRIORITY' if target=='P1' else 'NORMAL',
                    revision=revision,expected_revision=expected_revision,evidence_id='SYNTHETIC:REGISTER',**extra)
    def observe(event,dt=1000,**extra):
        return dict(scope(),op='monitoring_observe',route_rule_id='ROUTE-A',observation=dict(
            observation_id=event,source_id='S',camera_id='C1',frame_id='frame:'+event,feature_space=SPACE,
            embedding=vec(0),detection_score=.99,face_size_px=64,**extra),time_record=dict(profile_version='good',raw_time=iso(T0+dt)),policy=POLICY)
    def query(target='P1',**extra):
        return call(dict(scope(),op='monitoring_timeline_query',person_id=target,from_unix_ms=T0,to_unix_ms=now+1,**extra))
    try:
        call(dict(op='activity_sync',activity_id='A',activity_num='NUM-A',venue_id='V',config_version=1,expected_config_version=0,
                  configuration=dict(output_mode='UE_CAMERA_EVENTS')))
        call(dict(op='activity_bindings',activity_id='A',config_version=1,binding_version=1,expected_binding_version=0,
                  bindings=[dict(source_id='S',device_id='C1',kind='CAMERA',route_rule_id='ROUTE-A',camera_name='10136看台A1下入口1')]))
        call(dict(op='activity_monitoring',activity_id='A',config_version=1,binding_version=1,monitoring_version=1,expected_monitoring_version=0,enabled=True))
        calibration=profile();calibration['profile']['camera_id']='C1';call(calibration)
        registration=person(person_name='模拟重点人员甲',profile_photo_url='https://photos.example.test/profiles/P1.jpg',
                            templates=[dict(template_id='SYNTHETIC:P1',feature_space=SPACE,embedding=vec(0))])
        registered=call(registration)
        check('registered_business_person_details_are_preserved',registered['person'].get('person_name')=='模拟重点人员甲'
              and registered['person'].get('profile_photo_url')=='https://photos.example.test/profiles/P1.jpg')
        first=observe('capture-1',capture_photo_url='https://photos.example.test/captures/1.jpg')
        assert call(first)['status']=='ACCEPTED'
        assert call(observe('capture-2',dt=2000,capture_photo_url='https://photos.example.test/captures/2.jpg'))['status']=='ACCEPTED'
        assert call(observe('capture-legacy',dt=3000))['status']=='ACCEPTED'
        result=query();rows=result['timeline']
        check('backend_query_returns_person_summary',result['person']==dict(person_id='P1',person_name='模拟重点人员甲',
              profile_photo_url='https://photos.example.test/profiles/P1.jpg',category='PRIORITY',is_priority=True,person_revision=1))
        check('each_capture_has_its_own_photo_and_legacy_null',[r['capture_photo_url'] for r in rows]==[
              'https://photos.example.test/captures/1.jpg','https://photos.example.test/captures/2.jpg',None])
        check('camera_names_and_times_stay_attached_to_photo',rows[0]['camera_name']=='10136看台A1下入口1' and rows[0]['captured_at_unix_ms']==T0+1000)
        check('no_embedding_or_input_payload_exposed',not any(s in json.dumps(result) for s in ['embedding','templates','feature_space']))
        check('pull_does_not_enable_push',call(dict(op='push_status'))['push']['enabled'] is False and call(dict(op='push_status'))['push']['pending']==0)
        retry=call(first)
        check('same_capture_retry_is_idempotent',retry['duplicate'] is True and query()['count']==3)
        changed=json.loads(json.dumps(first));changed['observation']['capture_photo_url']='https://photos.example.test/captures/replaced.jpg'
        check('changed_photo_for_same_capture_rejected',call(changed,False)['error_code']=='SOURCE_CONFLICT')
        for index,url in enumerate(['file:///C:/face.jpg','javascript:alert(1)','data:image/jpeg;base64,AA','/relative.jpg','https://','https://u:p@example.test/face.jpg','https://example.test/line\nbreak.jpg',123,'https://example.test/back\\slash','https://:443/photo.jpg','https://:/photo.jpg']):
            check('invalid_photo_url_rejected_'+str(index),not call(observe('bad-'+str(index),capture_photo_url=url),False)['ok'])
        call(person(revision=2,expected_revision=1,person_name='模拟重点人员甲更新'))
        updated=query()['person']
        check('person_update_preserves_omitted_photo',updated['person_name']=='模拟重点人员甲更新' and updated['profile_photo_url']=='https://photos.example.test/profiles/P1.jpg')
        check('old_registration_retry_still_idempotent',call(registration)['duplicate'] is True)
        call(person('P2',person_name='模拟普通人员乙'))
        assert call(observe('live-photo',dt=8000,capture_photo_url='https://photos.example.test/captures/live.jpg'))['status']=='ACCEPTED'
        call(dict(scope(),op='association_correct',source_id='S',observation_id='capture-1',target_id='P2',evidence_id='SYNTHETIC:CORRECTION'))
        moved=query('P2')
        check('correction_returns_current_person_with_original_capture_photo',moved['person']['person_name']=='模拟普通人员乙'
              and moved['is_priority'] is False and moved['timeline'][0]['capture_photo_url']=='https://photos.example.test/captures/1.jpg')
        held=call(observe('held-photo',dt=9000,capture_photo_url='https://photos.example.test/captures/held.jpg'))
        assert held['status']=='HOLD' and held['camera_event'] is None
        call(dict(scope(),op='association_correct',source_id='S',observation_id='held-photo',target_id='P2',evidence_id='SYNTHETIC:CONFIRMED-LATER'))
        confirmed=query('P2')
        check('hold_then_manual_confirmation_keeps_original_photo',any(row['observation_id']=='held-photo' and row['capture_photo_url']=='https://photos.example.test/captures/held.jpg' for row in confirmed['timeline']))
        call(person(revision=3,expected_revision=2,person_name=None,profile_photo_url=None))
        check('explicit_null_clears_person_display_fields',query()['person']['person_name'] is None and query()['person']['profile_photo_url'] is None)
        empty=query('UNKNOWN')
        check('unknown_person_has_explicit_nulls_and_empty_timeline',empty['person']['person_name'] is None and empty['person']['profile_photo_url'] is None and empty['timeline']==[])
        now=T0+604800000+5000
        call(dict(op='retention_run',dry_run=False))
        live=query()
        check('retention_preserves_live_photo_without_resurrecting_old_capture',live['count']==1 and live['timeline'][0]['capture_photo_url']=='https://photos.example.test/captures/live.jpg')
        (a.output/'example-response.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    except BaseException as error:failure=repr(error)
    finally:
        process.stdin.close();process.wait(timeout=20)
        (a.output/'calls.json').write_text(json.dumps(calls,ensure_ascii=False,indent=2),encoding='utf-8')
        summary=dict(passed=sum(c['passed'] for c in checks),checks=checks,failure=failure)
        (a.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(summary,ensure_ascii=False,indent=2))
    if failure:raise SystemExit(1)

if __name__=='__main__':main()
