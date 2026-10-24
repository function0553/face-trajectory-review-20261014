"""Synthetic v4.1 regression against a real worker, restart and isolated SQLite."""
import argparse
import base64
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

P2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P2 / 'evidence/2026-10-10-activity-retention'))
from verify_core import T0, SPACE, POLICY, clock, iso, profile, scope, vec


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    database = args.output / 'isolated.sqlite'
    now = T0 + 100000
    initial_now = now
    checks = []
    process = None
    # A real tiny PNG, not an image URL or the enrollment photograph.
    photo = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='

    def start():
        return subprocess.Popen([str(args.worker.resolve()), '--state-file', str(database.resolve()), '--push-results', 'true'],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
    def stop():
        process.stdin.close(); process.wait(timeout=20)
    def call(body, ok=True):
        request = dict(body, _trusted_clock=clock(now))
        process.stdin.write(json.dumps(request, ensure_ascii=False) + '\n'); process.stdin.flush()
        line = process.stdout.readline()
        if not line:
            raise RuntimeError(process.stderr.read())
        result = json.loads(line)
        if result.get('ok') != ok:
            raise AssertionError((body['op'], result))
        return result
    def check(name, passed):
        checks.append(dict(name=name, passed=bool(passed)))
        if not passed:
            raise AssertionError(name)
    def control(op, activity='A', **extra):
        return call(dict(op='platform_v4_' + op, activity_id=activity, **extra))
    def head():
        return call(dict(op='push_next'))['event']
    def ack(event):
        call(dict(op='push_ack', event_id=event['event_id']))
    def person(pid='P', index=0, **extra):
        value = dict(personId=pid, personName='模拟' + pid, personIdNum='SYNTHETIC-ID-' + pid,
                     personPhoto='https://photos.example.test/' + pid + '.jpg', photo_sha256='SYNTHETIC:' + pid,
                     templates=[dict(template_id='photo:' + pid, feature_space=SPACE, embedding=vec(index))])
        value.update(extra)
        return value
    def sync(activity='A'):
        return control('sync', activity, body=dict(activityName='SYNTHETIC', activityNum='NUM-' + activity, activityState='0',
            start_unix_ms=T0, end_unix_ms=T0 + 10 * 86400000, cameras=[dict(deviceId=c, deviceName='后端' + c) for c in ('C1', 'C2')]))
    def observe(event, camera='C1', t=None, index=0, **extra):
        value = dict(scope(), op='monitoring_observe', route_rule_id='LOCAL-OLD-ROUTE',
            observation=dict(observation_id=event, source_id='LOCAL-SOURCE', camera_id=camera, camera_name='设备旧名',
                frame_id='frame:' + event, feature_space=SPACE, embedding=vec(index), detection_score=.99,
                face_size_px=64, capture_photo_base64=photo),
            time_record=dict(profile_version='good', raw_time=iso(T0 + 1000 if t is None else t)), policy=POLICY)
        value['observation'].update(extra)
        return value

    process = start()
    failure = None
    try:
        check('activity_sync_success', sync()['code'] == 200)
        check('activity_sync_idempotent', sync()['duplicate'])
        sync('B')
        control('person', person=person())
        registered = control('person', person=person())
        check('person_import_idempotent_and_lifetime', registered['duplicate'] and registered['expires_at_unix_ms'] == initial_now + 72 * 3600000)
        control('person', 'B', person=person(personName='另一个活动的模拟P'))
        check('same_person_id_isolated_by_activity', control('context')['context']['activity_id'] == 'A' and control('context', 'B')['context']['activity_id'] == 'B')
        check('empty_camera_array_cannot_clear_bindings', not call(dict(op='platform_v4_bindings',activity_id='A',body=dict(cameras=[])),False)['ok']
              and len(control('context')['context']['bindings'])==2)
        for c in ('C1', 'C2', 'UNBOUND'):
            value = profile(); value['profile']['camera_id'] = c; call(value)
        check('monitoring_starts_disabled', control('context')['context']['enabled'] is False)
        held = call(observe('disabled'))
        check('disabled_activity_does_not_push', held['status'] == 'HOLD' and head() is None)
        control('monitoring', body=dict(enabled=True))
        request = observe('first')
        check('uuid_source_and_internal_versions_are_resolved', call(request)['status'] == 'ACCEPTED')
        first = head(); event = first['person_track_event']
        check('flat_v41_wire_has_full_business_fields', len(event) == 14 and event['device_id'] == event['camera_id'] == 'C1' and 'source_id' not in event
              and event['person_name'] == '模拟P' and event['person_id_num'] == 'SYNTHETIC-ID-P')
        check('backend_name_and_original_capture_time', event['camera_name'] == '后端C1' and event['captured_at_unix_ms'] == T0 + 1000)
        check('base64_capture_is_preserved', event['capture_photo_url'] == photo and base64.b64decode(photo).startswith(b'\x89PNG'))
        check('same_event_retry_is_idempotent', call(request)['duplicate'] and head() == first)
        call(observe('cross-camera-boundary', 'C2', T0 + 6000))
        check('cross_camera_5000ms_inclusive_dedup', head() == first)
        stop(); process = start()
        check('restart_preserves_identical_pending_body', head() == first)
        control('bindings', body=dict(cameras=[dict(deviceId='C2', deviceName='后端新C2'), dict(deviceId='C1', deviceName='后端新C1')]))
        check('queued_name_does_not_change_after_rename', head() == first)
        check('retry_survives_binding_revision_change', call(request)['duplicate'])
        ack(first)
        check('dedup_survives_restart', head() is None)
        call(observe('after-window', 'C2', T0 + 6001))
        second = head()
        check('after_5000ms_can_report_again', second['person_track_event']['observation_id'] == 'after-window'
              and second['person_track_event']['camera_name'] == '后端新C2')
        ack(second)
        check('unbound_camera_is_held', call(observe('unbound', 'UNBOUND', T0 + 15000))['status'] == 'HOLD' and head() is None)
        call(observe('anonymous', t=T0 + 20000, index=3))
        check('unknown_person_does_not_push', head() is None)
        check('missing_image_rolls_back_recognition', not call(observe('missing-image', t=T0 + 20000, capture_photo_base64=None), False)['ok'] and head() is None)
        check('invalid_base64_rolls_back_recognition', not call(observe('bad-image', t=T0 + 20000, capture_photo_base64='not-an-image'), False)['ok'] and head() is None)
        outside = call(observe('outside-time', t=T0 - 1))
        check('capture_outside_activity_window_is_held', outside['status'] == 'HOLD' and 'ACTIVITY_OUTSIDE_TIME_WINDOW' in outside['reason_codes'])
        # A real correction retains the capture and updates the name/ID snapshot.
        control('person', person=person('Q', 1))
        correction = dict(scope(), op='association_correct', source_id='C1', observation_id='first', target_id='Q', evidence_id='SYNTHETIC:verified')
        call(correction)
        corrected = head(); wire = corrected['person_track_event']
        check('correction_uses_new_identity_snapshot', wire['revision'] == 2 and wire['person_id'] == 'Q' and wire['person_name'] == '模拟Q'
              and wire['person_id_num'] == 'SYNTHETIC-ID-Q' and wire['capture_photo_url'] == photo)
        ack(corrected)
        # Correcting the former representative can promote a suppressed P image.
        while head() is not None:
            ack(head())
        call(dict(correction, target_id=None))
        retract = head()['person_track_event']
        check('retract_has_null_identity_fields', retract['event_type'] == 'RETRACT' and retract['person_id'] is None
              and retract['person_name'] is None and retract['person_id_num'] is None)
        # Leave a pending event through both expiry and restart.
        pending = head()
        now = initial_now + 3600000
        updated=control('person',person=person(personName='修改后的模拟人员'))
        check('same_photo_metadata_update_is_atomic_and_does_not_extend_expiry',updated['recognition_ready'] and updated['expires_at_unix_ms']==registered['expires_at_unix_ms'])
        check('duplicate_import_does_not_extend_72h', control('person', person=person())['expires_at_unix_ms'] == registered['expires_at_unix_ms'])
        call(dict(scope(),op='remove_template',template_id='photo:P'))
        check('missing_template_is_rebuilt_instead_of_false_ready',control('person',person=person())['recognition_ready'])
        now = initial_now + 72 * 3600000
        call(dict(op='platform_v4_expire'))
        check('pending_event_survives_person_expiry', head() == pending)
        check('expired_original_import_does_not_resurrect', not call(dict(op='platform_v4_person',activity_id='A',person=person()),False)['ok'])
        stop(); process = start()
        check('pending_event_survives_expiry_and_restart', head() == pending)
        stop()
        with sqlite3.connect(database) as db:
            def count(namespace):
                return db.execute('SELECT count(*) FROM state WHERE ns=?', (json.dumps(['activity', 'A', namespace], separators=(',', ':')),)).fetchone()[0]
            check('expired_person_records_removed', count('monitoring_person') == 0 and count('platform_person_import') == 0 and count('monitoring_person_revision') == 0)
            templates = db.execute('SELECT value FROM state WHERE ns=?', (json.dumps(['activity', 'A', 'template'], separators=(',', ':')),)).fetchall()
            check('expired_recognition_templates_removed', all(json.loads(row[0])['person_entity_id'] not in ('P', 'Q') for row in templates))
            for namespace in ('person_track_state', 'person_track_candidate'):
                rows = db.execute('SELECT value FROM state WHERE ns=?', (json.dumps(['activity', 'A', namespace], separators=(',', ':')),)).fetchall()
                check('expired_identity_snapshot_scrubbed_' + namespace, all('SYNTHETIC-ID-' not in row[0] for row in rows))
        process = None
    except Exception as error:
        failure = repr(error)
        raise
    finally:
        if process is not None and process.poll() is None:
            stop()
        report = dict(synthetic_only=True, checks=checks, passed=sum(c['passed'] for c in checks), total=len(checks), failure=failure)
        (args.output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(dict(passed=report['passed'], total=report['total'], failure=failure), ensure_ascii=False))


if __name__ == '__main__':
    main()
