"""Real worker query contract: named-camera timelines without routes or query side effects."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

P2=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(P2/'evidence/2026-10-10-activity-retention'))
from verify_core import T0, SPACE, POLICY, vec, iso, scope, clock


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--worker',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    process=subprocess.Popen([str(a.worker.resolve()),'--state-file',str((a.output/'isolated.sqlite').resolve()),'--push-results','true'],
                             stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
    calls=[];checks=[];failure=None;now=T0+100000
    def call(body,ok=True):
        request=dict(body,_trusted_clock=clock(now))
        process.stdin.write(json.dumps(request,ensure_ascii=False)+'\n');process.stdin.flush()
        line=process.stdout.readline()
        if not line:raise RuntimeError(process.stderr.read())
        r=json.loads(line);calls.append(dict(request=request,response=r))
        if r.get('ok')!=ok:raise AssertionError((body['op'],r))
        return r
    def check(name,value):
        checks.append(dict(name=name,passed=bool(value)))
        if not value:raise AssertionError(name)
    def query(person='DAMAI-P',**more):
        return dict(scope(),op='monitoring_timeline_query',person_id=person,from_unix_ms=T0,to_unix_ms=T0+10000,**more)
    def add(event,dt):
        return call(dict(scope(),op='monitoring_observe',route_rule_id='ROUTE-A',
                         observation=dict(observation_id=event,source_id='S',camera_id='C1',frame_id='frame:'+event,
                                          feature_space=SPACE,embedding=vec(0),detection_score=.99,face_size_px=64),
                         time_record=dict(profile_version='good',raw_time=iso(T0+dt)),policy=POLICY))
    try:
        original=P2/'evidence/2026-10-21-ue-camera-events/green-02/calls.json'
        for item in json.loads(original.read_text(encoding='utf-8')):
            op=item['request']['op']
            if op=='retention_run':break
            if op in ['activity_sync','activity_bindings','activity_monitoring','clock_profile_put','monitoring_person_put','monitoring_observe'] and item['response'].get('ok'):
                call({k:v for k,v in item['request'].items() if not k.startswith('_')})
        before=call(dict(op='push_status'))['push']['pending']
        result=call(query())
        rows=result['timeline']
        check('device_names_and_capture_timeline_return_without_routes',[r['observation_id'] for r in rows]==['first','repeat','live-during-prune']
              and all(r['camera_name']=='南门检票大棚' and r['camera_id']=='C1' for r in rows))
        check('priority_follows_uploaded_person_data',result['person_id']=='DAMAI-P' and result['is_priority'] is True and result['category']=='PRIORITY')
        check('query_does_not_enqueue_or_subscribe',call(dict(op='push_status'))['push']['pending']==before)
        check('no_model_or_route_fields','route_waypoints' not in json.dumps(result) and 'model' not in json.dumps(rows))
        add('late-arrival',1500)
        result=call(query())
        check('timeline_uses_capture_order_not_ingest_order',[r['observation_id'] for r in result['timeline']]==['first','late-arrival','repeat','live-during-prune'])
        narrow=dict(query(),from_unix_ms=T0+1500,to_unix_ms=T0+2000)
        check('time_filter_is_start_inclusive_end_exclusive',[r['observation_id'] for r in call(narrow)['timeline']]==['late-arrival'])
        first=call(query(limit=2));cursor=first['next_after']
        check('bounded_page_explicitly_reports_more',first['has_more'] is True and [r['observation_id'] for r in first['timeline']]==['first','late-arrival'])
        second=call(query(limit=2,after=cursor,expected_association_revision=first['association_revision']))
        check('next_page_has_no_missing_or_repeated_rows',second['has_more'] is False and [r['observation_id'] for r in second['timeline']]==['repeat','live-during-prune'])
        add('another-at-same-time',2000)
        stale=call(query(limit=2,after=cursor,expected_association_revision=first['association_revision']),False)
        check('stale_pagination_requires_refresh',stale.get('error_code')=='VERSION_CONFLICT')
        ordered=call(query())['timeline']
        check('equal_timestamps_have_stable_source_event_tie_break',[r['observation_id'] for r in ordered if r['captured_at_unix_ms']==T0+2000]==['another-at-same-time','repeat'])
        for limit in [0,1001,True,1.5]:
            check('invalid_limit_rejected_'+str(limit),not call(query(limit=limit),False)['ok'])
        check('invalid_time_window_rejected',not call(dict(query(),to_unix_ms=T0),False)['ok'])
        empty=call(query('NO-APPEARANCES'))
        check('person_with_no_observations_returns_empty_list',empty['timeline']==[] and empty['has_more'] is False)
        json_id='["external-id-source","external-id-value"]'
        add(json_id,6500)
        check('external_json_shaped_observation_id_round_trips_once',any(r['observation_id']==json_id and r['source_id']=='S' for r in call(query())['timeline']))
        # Source identity corrections are authoritative for a query, not the
        # original appearance notification's person snapshot.
        call(dict(scope(),op='association_correct',source_id='S',observation_id='first',target_id='DAMAI-N',evidence_id='SYNTHETIC:independent-correction'))
        corrected=call(query());moved=call(query('DAMAI-N'))
        check('corrected_identity_removed_from_old_timeline',all(r['observation_id']!='first' for r in corrected['timeline']))
        check('corrected_identity_is_in_new_timeline',any(r['observation_id']=='first' and r['camera_name']=='南门检票大棚' for r in moved['timeline']))
        call(dict(scope(),op='monitoring_person_put',person_id='DAMAI-N',category='NORMAL',revision=3,expected_revision=2,evidence_id='SYNTHETIC:updated-upload'))
        check('timeline_priority_uses_current_uploaded_category',call(query('DAMAI-N'))['is_priority'] is False)
        now=T0+604800000+2000
        retained=call(dict(query(),to_unix_ms=now+1))
        check('query_enforces_retention_without_physical_delete',all(r['captured_at_unix_ms']>=T0+2000 for r in retained['timeline'])
              and retained['history_pruned'] is True and retained['effective_from_unix_ms']==T0+2000)
        (a.output/'example-request.json').write_text(json.dumps({k:v for k,v in query().items() if k!='op'},ensure_ascii=False,indent=2),encoding='utf-8')
        (a.output/'example-response.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    except BaseException as exc:failure=repr(exc)
    finally:
        process.stdin.close();process.wait(timeout=20)
        (a.output/'calls.json').write_text(json.dumps(calls,ensure_ascii=False,indent=2),encoding='utf-8')
        result=dict(passed=sum(c['passed'] for c in checks),checks=checks,failure=failure,scope='Local real worker/fresh SQLite; synthetic persons and clocks')
        (a.output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False,indent=2))
    if failure:raise SystemExit(1)


if __name__=='__main__':main()
