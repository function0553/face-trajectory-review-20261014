"""Regression for original capture-source clock validation before UUID routing.

Real worker and isolated SQLite; synthetic identities/calibration and image only.
"""
import argparse
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
    checks, calls = [], []
    process = None
    photo = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='
    def start():
        return subprocess.Popen([str(args.worker.resolve()), '--state-file', str(database.resolve()), '--push-results', 'true'],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
    def stop():
        process.stdin.close(); process.wait(timeout=20)
    def call(body):
        process.stdin.write(json.dumps(dict(body, _trusted_clock=clock(T0 + 100000))) + '\n'); process.stdin.flush()
        line = process.stdout.readline()
        if not line:
            raise RuntimeError(process.stderr.read())
        result = json.loads(line)
        calls.append(dict(request=body, response=result))
        return result
    def good(body):
        result = call(body)
        if result.get('ok') is not True:
            raise AssertionError((body['op'], result))
        return result
    def check(name, value):
        checks.append(dict(name=name, passed=bool(value)))
        if not value:
            raise AssertionError(name)
    def observation(event, source='LOCAL-SOURCE', include_source=True):
        record = dict(profile_version='good', raw_time=iso(T0 + 1000))
        if include_source:
            record['source_id'] = source
        return dict(scope(), op='monitoring_observe', route_rule_id='LOCAL-ROUTE',
                    observation=dict(observation_id=event, source_id=source, camera_id='C1', frame_id='frame:' + event,
                        feature_space=SPACE, embedding=vec(0), detection_score=.99, face_size_px=64, capture_photo_base64=photo),
                    time_record=record, policy=POLICY)
    def head():
        return good(dict(op='push_next'))['event']
    failure = None
    process = start()
    try:
        good(dict(op='platform_v4_sync', activity_id='A', body=dict(activityName='SYNTHETIC', activityNum='NUM-A',
            activityState='0', start_unix_ms=T0, end_unix_ms=T0 + 86400000, cameras=[dict(deviceId='C1', deviceName='入口1')])))
        good(dict(op='platform_v4_person', activity_id='A', person=dict(personId='P', personName='模拟人员',
            personIdNum='SYNTHETIC-ID', personPhoto='https://photos.example.test/P.png', photo_sha256='SYNTHETIC',
            templates=[dict(template_id='photo:P', feature_space=SPACE, embedding=vec(0))])))
        good(dict(op='platform_v4_monitoring', activity_id='A', body=dict(enabled=True)))
        calibration = profile(error=3, offset=25); calibration['profile']['camera_id'] = 'C1'; good(calibration)
        expected = good(dict(op='clock_normalize', camera_id='C1', source_id='LOCAL-SOURCE', profile_version='good',
                             raw_time=iso(T0 + 1000), time_policy='CAMERA_CAPTURE_60S'))['normalization']
        request = observation('explicit-source')
        accepted = call(request)
        check('matching_original_capture_source_is_accepted', accepted.get('ok') and accepted.get('status') == 'ACCEPTED')
        check('first_clock_result_and_full_provenance_are_preserved', accepted['normalization'] == expected)
        pending = head(); event = pending['person_track_event']
        check('outbound_camera_identity_and_person_unchanged', event['device_id'] == event['camera_id'] == 'C1'
              and 'source_id' not in event and event['person_id'] == 'P' and event['person_name'] == '模拟人员'
              and event['person_id_num'] == 'SYNTHETIC-ID')
        check('original_capture_time_and_error_bound_preserved', event['captured_at_unix_ms'] == T0 + 1000
              and event['clock_uncertainty_ms'] == expected['clock_uncertainty_ms'])
        with sqlite3.connect(database) as db:
            namespace = json.dumps(['activity', 'A', 'activity_intake'], separators=(',', ':'))
            key = json.dumps(['C1', 'explicit-source'], separators=(',', ':'))
            record = json.loads(db.execute('SELECT value FROM state WHERE ns=? AND key=?', (namespace, key)).fetchone()[0])
            check('original_time_record_source_is_not_rewritten', record['content']['time_record']['source_id'] == 'LOCAL-SOURCE')
        check('same_request_retry_is_idempotent', good(request)['duplicate'] is True and head() == pending)
        stop(); process = start()
        check('restart_retry_preserves_queued_body', good(request)['duplicate'] is True and head() == pending)
        good(dict(op='push_ack', event_id=pending['event_id']))
        missing = good(observation('optional-source-omitted', include_source=False))
        check('time_record_without_optional_source_still_works', missing['status'] == 'ACCEPTED' and missing['normalization'] == expected)
        equal = good(observation('source-already-camera-uuid', source='C1'))
        check('source_already_equal_to_uuid_still_works', equal['status'] == 'ACCEPTED' and equal['normalization'] == expected)
        mismatch = observation('invalid-source')
        mismatch['time_record']['source_id'] = 'WRONG-SOURCE'
        rejected = call(mismatch)
        check('actual_original_source_mismatch_is_rejected', rejected.get('ok') is False and rejected.get('error') == 'time_record source differs from observation')
        check('source_rejection_does_not_commit_observation', good(observation('invalid-source'))['status'] == 'ACCEPTED')
        disguised = observation('uuid-must-not-bypass-source-validation')
        disguised['time_record']['source_id'] = 'C1'
        check('uuid_in_time_record_cannot_bypass_original_source_validation', call(disguised).get('ok') is False)
        injected = copy.deepcopy(mismatch); injected['observation']['observation_id'] = 'injected-normalization'; injected['_normalization'] = expected
        check('caller_normalization_cannot_bypass_original_source_validation', call(injected).get('ok') is False)
        wrong_camera = observation('wrong-camera'); wrong_camera['time_record']['camera_id'] = 'OTHER-CAMERA'
        check('original_time_record_camera_mismatch_is_rejected', call(wrong_camera).get('error') == 'time_record camera differs from observation')
        no_time = observation('no-time'); no_time['time_record'] = None
        held = good(no_time)
        check('missing_capture_time_remains_held', held['status'] == 'HOLD' and held['time_admitted'] is False)
        unknown = observation('unknown-clock'); unknown['time_record']['profile_version'] = 'unknown'
        held = good(unknown)
        check('unknown_clock_profile_remains_held', held['status'] == 'HOLD' and 'UNKNOWN_CLOCK_PROFILE' in held['reason_codes'])
        future = observation('future-time'); future['time_record']['raw_time'] = iso(T0 + 160001)
        held = good(future)
        check('trusted_future_time_guard_is_preserved', held['status'] == 'HOLD' and 'FUTURE_CAPTURE_TIME_OVER_60S' in held['reason_codes'])
        bad = profile(name='excess-error', error=60001); bad['profile']['camera_id'] = 'C1'; good(bad)
        uncertain = observation('excess-error'); uncertain['time_record']['profile_version'] = 'excess-error'
        held = good(uncertain)
        check('clock_error_limit_is_preserved', held['status'] == 'HOLD' and held['time_admitted'] is False)
        check('held_and_duplicate_observations_do_not_enqueue_events', head() is None)
    except Exception as error:
        failure = repr(error)
        raise
    finally:
        if process is not None and process.poll() is None:
            stop()
        report = dict(passed=sum(c['passed'] for c in checks), total=len(checks), checks=checks, failure=failure,
                      scope='Local real worker with isolated SQLite; synthetic camera clock and faces; no production changes')
        (args.output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        (args.output / 'requests-and-responses.json').write_text(json.dumps(calls, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(dict(passed=report['passed'], total=report['total'], failure=failure)))


if __name__ == '__main__':
    main()
